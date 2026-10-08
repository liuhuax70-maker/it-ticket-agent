# permission-aware-rag

企业级**权限感知** RAG 知识库平台：把「接入文档 → 切分 → 向量化 → 混合检索 → 大模型生成 → 带引用作答」端到端跑通，并把权限过滤、异步通道、评测接缝预留在正确的位置。模型走 DeepSeek，官方 API 与本地 OpenAI 兼容端点（Ollama / vLLM）**双模可切换**。

技术栈：`FastAPI · LangGraph · Postgres · OpenSearch · Milvus · Redis · Kafka · LiteLLM · RAGAS · Langfuse · Keycloak/OPA · K8s · Terraform`

---

## 背景与动机

企业知识库问答的难点不在生成，而在**权限**。把文档接进向量库、让模型基于检索结果作答，是已经成熟的方案；难的是当一份文档只对某些人可见时，如何保证它**既能被有权限的人找到、又绝不会出现在无权者的答案里**——而且不能靠「先检索再在应用层过滤」，那样越权文档会先占满 top_k，把有权访问的文档挤出去。

本项目给出的做法是把权限**编译成存储层的过滤表达式**（Milvus `expr` / OpenSearch `filter`），让过滤发生在召回之前。围绕这一条，附带解决几个同类问题：

- **引用必须可溯源**：每个分块携带 `char_start/char_end`，切分保证 `content[char_start:char_end] == chunk.text`，答案里的 `[n]` 能精确跳回原文。
- **不知道就拒答**：检索为空或相关性不足时直接拒答、不调用 LLM，并**清空引用**——挂着引用的拒答会误导用户以为「资料里写了」。
- **旧制度不能被当现行制度引用**：被取代的文档通过声明式生命周期标记，库层过滤掉。

适合：需要在多租户/多部门隔离下落地 RAG 的团队。技术栈不绑定本项目，可只借鉴权限下推与验收矩阵两块。

---

## 功能特性

1. **权限过滤下沉到存储层**：ACL 编译成 Milvus `expr` 与 OpenSearch `filter`，越权文档不占用 top_k。
2. **可定位引用**：答案 `citations` 带 `char_start/char_end/section_path`，可回跳原文高亮。
3. **检索为空即拒答**：不调用 LLM，拒答时 `citations` 恒为空。
4. **混合检索**：BM25（OpenSearch）+ 向量（Milvus），RRF 融合免标定，可选重排（默认关闭）。
5. **多租户隔离**：`tenant/department/owner/visibility` 由网关从已校验身份注入，客户端不可指定。
6. **文档失效管理**：声明式生命周期（`configs/corpus/lifecycle.yaml`），废止文档不参与检索且台账可见。
7. **可回归的评测**：L1 确定性指标（越权、拒答、命中、排序）不依赖裁判模型，L2 用 RAGAS 打答案质量分。

---

## 系统架构

```
                    ┌──────────────── apps/api-gateway :8000 ────────────────┐
   浏览器 / 调用方 ──▶│ 身份(Keycloak JWT 占位) → 限流(Redis) → 审计 → 路由转发 │
                    └───────────────────────┬───────────────────────────────┘
                                            │  /chat
                    ┌───────────────────────▼───────────────────────────────┐
                    │        services/query-orchestrator :8001              │
                    │  LangGraph: cache → rewrite → route → retrieve        │
                    │             → rerank → generate → guard → cache_store  │
                    │         （检索为空 → refuse，不调用 LLM）              │
                    └───────┬───────────────────────────────┬───────────────┘
                            │ /search /rerank               │ /generate /complete
        ┌───────────────────▼──────────────┐   ┌────────────▼──────────────────┐
        │   services/retrieval :8002       │   │  services/model-gateway :8003 │
        │  BM25(OpenSearch) + 向量(Milvus) │   │  LiteLLM + 降级链 + 配额       │
        │  + RRF 融合 + ACL 过滤下沉        │   │  DeepSeek 官方 / 本地端点      │
        └───────┬───────────────┬──────────┘   └────────────┬──────────────────┘
                │               │                             │
          OpenSearch         Milvus                     DeepSeek / Ollama
                ▲               ▲
                │               │
        ┌───────┴───────────────┴──────────┐
        │    services/indexing :8005        │◀── 同步直连（USE_KAFKA=false）或 chunk-events
        │  embedding → 双写 Milvus+OpenSearch│
        └──────────────────────────────────┘
                ▲
                │  /index
        ┌───────┴──────────────────────────┐
        │    services/ingestion :8004       │  解析 → 切分 → Postgres(元数据/ACL)
        │  ChunkSink: HTTP直连 | Kafka占位   │
        └──────────────────────────────────┘

  旁路：services/authz(策略控制面) · services/eval(RAGAS) · services/feedback(反馈/坏例)
```

| 服务 | 端口 | 职责 | 不做什么 |
| --- | --- | --- | --- |
| `apps/api-gateway` | 8000 | 鉴权、限流、审计、路由转发、文件上传 | 不参与 RAG 逻辑 |
| `services/query-orchestrator` | 8001 | LangGraph 编排、引用映射、拒答 | 不直连任何存储 |
| `services/retrieval` | 8002 | 混合检索、RRF 融合、ACL 过滤下沉、重排 | 不生成答案 |
| `services/model-gateway` | 8003 | 模型路由、降级链、租户配额、向量化 | 不感知业务语义 |
| `services/ingestion` | 8004 | 解析、切分、元数据/ACL 落库、删除 | 不写检索索引 |
| `services/indexing` | 8005 | 向量化、写 Milvus + OpenSearch | 不写业务元数据 |
| `services/eval` | 8006 | RAGAS 跑批、回归报告 | 不参与在线链路 |
| `services/feedback` | 8007 | 反馈落库、坏例导出 | 不参与在线链路 |
| `services/authz` | 8008 | 策略包可视化、决策排障 | **不参与**请求链路（执行点在网关） |

**关键约束**（改动前先了解，详细取舍见 `docs/adr/`）：

1. 权限过滤只在存储层做，不在应用层裁剪 top_k。`docs/adr/0002`
2. 检索为空直接拒答、不调用 LLM；拒答一律清空 `citations`。`docs/adr/0003`
3. `tenant/department/owner` 只能由网关从身份注入；`visibility=private` 缺 `owner` 直接 422，不做默认值兜底。`docs/adr/0004`
4. 查询缓存键必须含身份维度 `(tenant_id, department_id, user_id, mode, top_k, temperature, query)`，否则同租户跨部门会共用答案、检索层 ACL 被整段绕过。`docs/adr/0006`
5. 评测固定传 `use_cache=false`（生产默认不变），否则命中响应缺 contexts 会让检索侧指标分母浮动、两轮不可比。`docs/adr/0005`
6. 提示注入：资料声明为数据 + 结构转义 + 检测告警，检测**不阻断**。`docs/adr/0007`

---

## 快速开始

### 环境要求

- Python 3.11+
- Docker / Docker Compose
- 可选：本机 Ollama（无 DeepSeek 密钥时用它跑通闭环）

### 1. 启动依赖

```bash
cp .env.example .env          # 按需修改端口与模型配置
docker compose up -d          # Postgres / Redis / OpenSearch / Milvus(+etcd+MinIO)，等价 make infra-up
# 需要 Kafka / Keycloak+OPA / Langfuse / 监控时：
docker compose --profile streaming --profile authz --profile observability --profile monitoring up -d
```

### 2. 安装与建表

```bash
python -m venv .venv && .venv/Scripts/activate    # Windows；Linux 用 source .venv/bin/activate
pip install -e ".[dev]"
python scripts/migrate.py                         # Alembic 建表
```

### 3. 配置模型（DeepSeek 双模）

```ini
# 方式 A：DeepSeek 官方 API（默认路径）
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=sk-xxxx

# 方式 B：本机 Ollama（推荐本地开发，无需密钥）
LLM_PROVIDER=local
LOCAL_LLM_MODEL=qwen3.5:4b
LOCAL_LLM_API_STYLE=ollama   # 走 Ollama 原生接口
LOCAL_LLM_THINK=false        # 思考型模型必须关思考链，见 FAQ
```

### 4. 启动服务

```bash
python scripts/dev_services.py     # 一键前台启动 6 个在线服务（Ctrl+C 全部退出），等价 make run-all
# 或按需单独启动（**务必用装了依赖的解释器**，见下）
python -m uvicorn app.main:app --app-dir services/retrieval --port 8002 --reload
```

> **必须用 `.venv` 的解释器启动**：`model-gateway` 依赖 `litellm`，它只装在项目虚拟环境里。
> 用系统 Python 启动会得到 `No module named 'litellm'`，进而所有 `/chat` 返回 502，
> 症状是"检索正常但生成失败"，很容易误判成模型问题。虚拟环境不存在时先 `make install`。
>
> ```bash
> # Windows（PowerShell）
> .venv\Scripts\python.exe -m uvicorn app.main:app --app-dir services/model-gateway --port 8003
> # macOS / Linux
> .venv/bin/python -m uvicorn app.main:app --app-dir services/model-gateway --port 8003
> ```

打开 <http://localhost:8000/ui/> 即为问答界面（零构建单页应用）。

前端所需的 OIDC 参数（issuer / client_id）由后端 `GET /ui-config` 在运行时下发，
**不要**在前端代码里硬编码——换部署环境时改 `KEYCLOAK_URL` / `KEYCLOAK_UI_CLIENT_ID` 即可。

### 5. 导入语料并验收

```bash
python scripts/seed.py               # 导入 data/corpus（幂等）
python scripts/verify_loop.py        # 全量验收 V1~V5（需要可用的 LLM 配置）
python scripts/verify_loop.py --skip-chat   # 只验健康、接入幂等与引用回查（不需要 LLM）
```

| 判据 | 内容 |
| --- | --- |
| V1 | 6 个服务 `/health` 正常，Milvus / OpenSearch 真实连通 |
| V2 | 语料接入成功且**可重复执行**（两次 chunk 数一致） |
| V3 | **检索级引用回查**：`content[char_start:char_end] == hit.text` 逐字成立 |
| V4 | 正样本返回非空答案 + 非空 `citations`，且引用区间与原文一致 |
| V5 | 负样本（文档里没有的问题）走拒答，`refused=true` 且 `citations=[]` |

### 6. 权限闭环验收（本项目主线）

上面 V1~V5 只覆盖单租户单部门；「权限感知」需用**真实身份**跑隔离矩阵：

```bash
docker compose --profile authz up -d   # Keycloak + OPA
make verify-permissions                # 清理历史产物 → 上传权限语料 → 跑隔离矩阵
```

判据全部用 doc_id 集合断言，不依赖模型措辞：

| 判据 | 内容 |
| --- | --- |
| P0 | 探针 `/health` 免鉴权可用；无令牌/伪造令牌访问 `/chat` 得 401；令牌声明被正确解析 |
| P1 | 按真实身份上传的文档，其 `tenant/department/visibility` **落库值与身份一致** |
| P2 | 4 个查询 × 5 个身份：每次回答的 `citations` 必须**完全落在该身份的可见集合内** |
| P3 | **存储层过滤硬证据**：以 engineering 身份检索 HR 文档原句 → 结果 0 条 HR；同句以 hr 身份 → 命中（对照组成立） |
| P4 | OPA 生效：无写角色写入返回 403，有写角色放行；其他租户看不到 default 租户任何文档 |

测试身份（realm 内置账号，语料在 `data/corpus_permissions/`）：

| 账号 | 租户 / 部门 | 角色 | 可见范围 |
| --- | --- | --- | --- |
| alice | default / hr | rag_user | hr_policy + 公共手册 |
| carol | default / hr | rag_user + **rag_writer** | 额外可见自己的 private 笔记 |
| bob | default / engineering | rag_user | eng_runbook + 公共手册 |
| erin | default / engineering | rag_user + **rag_writer** | 同 bob |
| dave | **tenant-b** / hr | rag_user | 租户隔离，一份都看不到 |

---

## 使用示例

问答（`AUTHZ_ENABLED=false` 时用固定身份；开启后带 Keycloak 令牌）：

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"query":"差旅住宿标准是多少","top_k":5}'
```

```json
{
  "answer": "一线城市住宿标准为每晚不超过 500 元 [1]。",
  "citations": [
    {
      "index": 1,
      "chunk_id": "d_830daea6::c003",
      "doc_id": "d_830daea6",
      "doc_title": "差旅管理制度",
      "chunk_index": 3,
      "section_path": "差旅费用 > 住宿",
      "char_start": 412,
      "char_end": 468,
      "score": 0.83,
      "snippet": "一线城市住宿费每晚不超过 500 元……"
    }
  ],
  "timings_ms": { "total": 688 },
  "refused": false,
  "cached": false,
  "model": "deepseek-chat",
  "trace_id": "…"
}
```

拒答时（检索为空或相关性不足）：

```json
{ "answer": "抱歉，知识库中没有能回答该问题的资料。", "citations": [], "refused": true }
```

上传文档：

```bash
# 文件上传
curl -X POST http://localhost:8000/documents/upload -F "file=@handbook.md"

# 按服务器路径接入（文件或目录）
curl -X POST http://localhost:8000/documents/ingest \
  -H "Content-Type: application/json" \
  -d '{"path":"./data/corpus","reindex":true}'
```

查询台账与删除：

```bash
curl "http://localhost:8000/documents?keyword=差旅"
curl -X DELETE http://localhost:8000/documents/d_830daea6   # 同步清 Milvus + OpenSearch + 元数据
```

---

## 配置说明

全部环境变量见 `.env.example`（字段名即 `Settings` 字段名，大小写不敏感）。常用的：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AUTHZ_ENABLED` | `false` | 关闭时走固定身份；开启后经 Keycloak 校验 JWT |
| `LLM_PROVIDER` | `deepseek` | `deepseek` / `local` |
| `DEEPSEEK_API_KEY` | 空 | 用官方 API 时必填 |
| `LOCAL_LLM_API_STYLE` | `openai` | `openai` / `ollama`；后者支持关闭思考链 |
| `LOCAL_LLM_THINK` | `false` | 思考型模型须为 `false`，否则输出预算耗在思维链、`content` 为空 |
| `ANSWER_PROMPT_VERSION` | `v4` | 提示模板版本（`configs/prompts/`）。**字段名无 `LLM_` 前缀**，写成 `LLM_ANSWER_PROMPT_VERSION` 会被静默忽略 |
| `EMBED_BACKEND` | `fastembed` | 本地 ONNX，无需外部服务；可切 `litellm` |
| `EMBED_MODEL` / `EMBED_DIM` | `BAAI/bge-small-zh-v1.5` / `512` | 改这两项需重建 Milvus 集合 |
| `RETRIEVE_MODE` | `hybrid` | `vector` / `keyword` / `hybrid` |
| `TOP_K` | `5` | 返回条数 |
| `RERANK_ENABLED` | `false` | 重排默认关闭（CPU 下延迟代价高，见第 11 节） |
| `CACHE_ENABLED` | `true` | 查询缓存 |
| `CACHE_VERSION` | `1` | **改动影响答案内容的配置后须调大**，让旧缓存失效（如换模型、改提示词、改切分参数） |
| `USE_KAFKA` | `false` | 置 `true` 走 ingestion→indexing 异步事件 |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | `500` / `80` | 切分参数 |
| `DATABASE_URL` | `postgresql+asyncpg://rag:rag@localhost:5432/rag` | 元数据与 ACL |
| `REDIS_URL` / `MILVUS_URI` / `OPENSEARCH_URL` | `redis://localhost:6379/0` / `http://localhost:19530` / `http://localhost:9200` | 存储连接 |
| `METRICS_TOKEN` | 空 | 配置后**所有**服务的 `/metrics` 都要求 Bearer 令牌 |

> 端口被占用时改 `.env` 里的 `REDIS_PORT` / `MILVUS_PORT` 等，compose 会按 `.env` 插值。

---

## API 与命令参考

### HTTP 接口

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| `GET` | `/health` | 免鉴权 | 探针 + 下游依赖连通性 |
| `POST` | `/chat` | 需登录 | 知识库问答，返回答案 + 引用 |
| `POST` | `/documents/ingest` | rag_writer | 按路径或文本接入 |
| `POST` | `/documents/upload` | rag_writer | 上传文件接入（`.md/.txt/.pdf`） |
| `GET` | `/documents` | 需登录 | 文档台账（支持 `keyword` 搜索） |
| `GET` | `/documents/jobs/{job_id}` | 需登录 | 接入任务状态 |
| `GET` | `/documents/{doc_id}` | 需登录 | 文档元数据 |
| `DELETE` | `/documents/{doc_id}` | rag_writer + 权限校验 | 合规删除（同步清索引与元数据） |
| `POST` | `/feedback` | 需登录 | 提交问答反馈 |
| `GET` | `/admin/stats` | rag_admin | 文档/分块统计 |
| `GET` | `/admin/models` | rag_admin | 模型清单与当前生效模型 |
| `GET` | `/admin/quotas/{tenant_id}` | rag_admin | 租户当日 token 用量与成本 |
| `GET` | `/admin/audit` | rag_admin | 访问审计记录 |
| `GET` | `/metrics` | 见配置 | Prometheus 文本格式 |
| `GET` | `/ui/` | 免鉴权 | 内置单页问答界面 |

### 脚本

| 命令 | 说明 |
| --- | --- |
| `python scripts/migrate.py` | Alembic 迁移（`--sql` 打印离线 SQL，不落库） |
| `python scripts/dev_services.py` | 前台拉起 6 个在线服务，Ctrl+C 全部退出 |
| `python scripts/seed.py` | 导入 `data/corpus`（幂等） |
| `python scripts/verify_loop.py` | 最小闭环验收 V1~V5 |
| `python scripts/verify_permissions.py` | 权限隔离矩阵验收 P0~P4 |
| `python scripts/rebuild_index.py` | 重建检索索引（改 embedding 维度 / schema 后使用） |
| `python scripts/check_corpus_consistency.py` | 语料一致性检查（规范值冲突、引用断链等） |
| `python scripts/test_all.py` | 全量测试（按服务分进程） |

---

## 目录结构

```
apps/           api-gateway（+ 预留 chat-ui / admin-console）
services/       query-orchestrator / retrieval / ingestion / indexing / model-gateway / eval / feedback / authz
packages/       共享库（contracts / common / llms / embeddings / search / vectorstores /
                retrievers / prompts / observability / security），不持有业务状态
pipelines/      离线任务：ingestion_dag（增量同步）· eval_dag（RAGAS 跑批）· cleanup_dag（留存/合规删除）
configs/        env / models(litellm.yaml) / prompts(版本化模板) / retrievers / tenants / corpus
infra/          docker · k8s（helm + base/overlays）· terraform · monitoring
migrations/     postgres（Alembic）· opensearch（索引模板）
scripts/        migrate / dev_services / seed / verify_loop / verify_permissions / test_all
eval_data/      golden.jsonl（黄金评测集）
data/corpus/    演示语料
```

依赖方向铁律：`contracts → common → llms/embeddings/search/vectorstores/retrievers/prompts/security → services → apps`。服务之间只通过 `packages/contracts` 的契约通信，**不允许跨服务 import 实现细节**。

---

## 开发指南

日常入口用 Makefile 目标（与 CI 门禁一致）：

```bash
make install          # 安装依赖（含 dev）
make test             # 全量测试（按服务分进程）
make test-packages    # 只跑共享库测试
make lint             # 静态检查（ruff）
make type-check       # 类型检查（mypy 逐模块跑，一次跑全仓会因同名 app 包歧义）
make fmt              # 统一格式
make fmt-check        # 格式检查
make verify           # 端到端闭环验收
```

其中 `lint`、`type-check`、`fmt-check`、`corpus-check` 是 CI 门禁项。

测试必须分进程：每个服务都有顶层 `app` 包，同一个 pytest 会话里 `app` 只会绑定到最先导入的那个服务。用 `make test`（等价 `python scripts/test_all.py`）。

评测与回归门禁：

```bash
make eval-fast        # 只跑 L1 确定性指标（分钟级，适合改参数后反复跑）
make eval             # 全量（L1 + L2 RAGAS）
make eval-gate        # 对比 configs/eval/baseline.json，指标不许变差
make eval-baseline    # 把当前报告冻结为新基线（显式接受现状，需 review）
```

门禁分两类：**零容忍不变量**（`leak_count` 越权、`forbidden_count` 注入得逞）必须为 0，字段缺失也算失败；**指标退化**按容差判定（检索类 0、拒答类 0.05、L2 0.10）。详见 `docs/adr/0005`。

---

## 部署

### 依赖的外部服务

| 服务 | 必需 | 用途 |
| --- | --- | --- |
| Postgres | 是 | 文档元数据、ACL、审计 |
| Milvus | 是 | 向量检索 |
| OpenSearch | 是 | BM25 关键词检索 |
| Redis | 是 | 查询缓存、限流、成本累计 |
| Kafka | 否 | `USE_KAFKA=true` 时启用；默认同步直连 |
| Keycloak + OPA | 否 | `AUTHZ_ENABLED=true` 时启用；默认固定身份 |
| Langfuse | 否 | 配置 `LANGFUSE_*` 后上报 trace |

### 基础设施与服务部署

```bash
# Terraform：依赖组件（PG/Redis/Milvus/OpenSearch/Kafka/Keycloak/OPA）
terraform -chdir=infra/terraform/envs/dev init && terraform -chdir=infra/terraform/envs/dev plan

# Kustomize：集群级共享资源（ConfigMap/NetworkPolicy/PDB）
kubectl kustomize infra/k8s/base

# Helm：业务服务工作负载
make image
helm template api-gateway infra/k8s/helm/rag-service -f infra/k8s/helm/values/api-gateway.yaml
```

### 监控

9 个服务全部暴露 `GET /metrics`（Prometheus 文本格式）。关键指标：

| 指标 | 类型 | 用途 |
| --- | --- | --- |
| `rag_acl_missing_total` | counter | **权限下推链路断裂次数，生产必须恒为 0** |
| `rag_answer_total` | counter | 拒答率与错误率（`outcome=answered/refused/error`） |
| `http_request_duration_seconds` | histogram | P95 / P99 |
| `rag_injection_suspected_total` | counter | 疑似提示注入次数（检测不阻断） |

抓取配置与告警规则在 `infra/monitoring/`；`RagAclMissing` 为绝对不变量，出现一次即告警。SLO 见 `docs/slo.md`。

### 当前状态与已知边界

| 项 | 现状 | 后续 |
| --- | --- | --- |
| 鉴权与授权 | Keycloak（JWT + JWKS，验签失败自动刷新）+ OPA（默认拒绝白名单），端到端验收通过 | 字段级/文档级授权、令牌静默刷新、生产 HTTPS + PKCE |
| Keycloak / OPA 开关 | 默认 `AUTHZ_ENABLED=false` 走固定身份 | 打开开关即启用，无需改代码 |
| Kafka | 仅占位，默认同步直连 | `USE_KAFKA=true` 切异步 |
| 角色白名单可见性 | `allowed_roles` 已入契约但未参与过滤 | 补存储层 schema/expr 与字段传递 |
| 查询规划 | 已实现（`DECOMPOSE_ENABLED`）；**默认关闭**，三版迭代实测无净增益 | 等评测集出现「RRF 排错、分解能纠」的具体案例 |
| Reranker | 已接线（bge-reranker-base）；**默认关闭**，开启后质量无变化、P50/P95 约 12x/21x（CPU） | 更强 reranker 或 GPU |
| 流式输出 | 一次性返回 + 思考中提示 | 三级 SSE 透传（LangGraph `astream` 已可提供节点级进度） |
| 解析格式 | Markdown / Txt / PDF（pypdf，无 OCR） | docx / html / OCR |
| 中文分词 | OpenSearch `standard` 分析器 | 换带 IK 插件的镜像并重建索引 |
| 入库吞吐 | 单文档 `/index` 约 20s（Milvus `flush` + OpenSearch `refresh`） | 批量写入 + 关闭同步 refresh |
| 上传内容校验 | 只做大小与非空校验，无 MIME 校验、无投毒检测 | 类型白名单与内容扫描 |
| 语义缓存 | 精确匹配 | 可升级为 embedding 相似度匹配（须同时保证身份隔离） |
| 数据失效管理 | 已实现：声明式生命周期 + 库层 `must_not` 过滤。残余：日期判定在入库时，跨失效日需重新入库 | 自动翻转改为可比较日期字段 + 查询时注入「今天」 |
| 提示注入 | 三层已实现（声明 + 转义 + 检测告警）+ 零容忍门禁。残余：检测只覆盖已知表达形式 | 模板改动时同步 `_FORGEABLE_SECTIONS` |
| 审计日志 | 已落库 `audit_logs`，队列+批量写，失败丢弃并告警、不影响业务请求 | 加保留期与自动归档；记录「访问了哪些文档」 |
| 成本核算 | 已折算 `rag_llm_cost_usd_total{model,tenant}`，单价会漂移以供应商账单为准 | 对接账单系统 |

> 实测（本机 Docker + CPU 推理）：`/chat` 端到端约 2–14s（检索 ~0.3s、生成 2.3–12.6s）；`/index` 单文档（10 分块）约 20s，瓶颈在 Milvus flush 与 OpenSearch refresh，不在向量化。

---

## 常见问题

**Q：模型一直拒答，或返回内容为空？**
思考型模型（`qwen3`、`deepseek-r1`）在 OpenAI 兼容接口下会把输出预算耗在思维链上，最终 `content` 为空。改用 `LOCAL_LLM_API_STYLE=ollama` + `LOCAL_LLM_THINK=false`（仅 Ollama 原生接口支持关闭思考链）。非思考型模型保持 `openai` 即可。

**Q：改了代码但答案没变？**
查询缓存。改动影响答案内容的配置（换作答模型、改提示词、改切分参数）后须调大 `CACHE_VERSION`，或临时置 `CACHE_ENABLED=false`。
例外：**作答模型已可逐请求指定**（`ChatRequest.model`），这类变化由缓存键里的 `model` 分量直接区分，不需要也不应该动 `CACHE_VERSION`。

**Q：问「你好」却列出一堆制度切片？**
`MIN_SCORE` 相关性阈值被改小或关掉了。无关提问与真实提问的向量分数实测分离在 `0.4077 / 0.4521` 之间（见 [ADR 0008](docs/adr/0008-relevance-threshold.md)），阈值 0.43 是这份语料上的最优切点。**换语料或换嵌入模型必须重新标定**，否则会静默误杀或静默失效。

**Q：问「请假什么流程？」说没资料，但问「请假」就有？**
已修（提示词 v6）。原因不是检索——两者命中的是同一批文档。v5 的判据是「资料里有没有*这个答案*」，而完整答案常需跨分节整合、且用户用词与文档用词不同（问「请假」而文档写「病假/事假/年假」），于是被误判成「资料没写」。v6 改为「有没有*可用事实*」。

**Q：新增了 `lifecycle` 字段但检索行为没变？**
Milvus schema 没有 alter（`enable_dynamic_field=False`），存量集合不会自动补列，须用 `python scripts/rebuild_index.py` 重建。OpenSearch 侧不用重建，`ensure_index` 会幂等 `put_mapping` 补齐。

**Q：启动 Prometheus 后所有服务 `/metrics` 都 403？**
配了 `METRICS_TOKEN` 后它是**全局生效**的，不只是网关，因此 `prometheus.yml` 的两个 job 都要带凭据。配了 token 却不带，就会集体 403。

**Q：为什么拒答时 `citations` 是空的？**
刻意如此。引用意味着「有资料支撑」，挂在拒答上会误导用户以为资料里写了答案。

**Q：端口 6379 / 19530 被占用？**
改 `.env` 里的 `REDIS_PORT` / `MILVUS_PORT` 等，compose 会按 `.env` 插值。

**Q：`verify_loop.py` 报「拒答率异常」但模型看起来正常？**
先用 `--skip-chat` 确认非 LLM 的三项（服务健康、接入幂等、引用回查）是否通过——它们不依赖模型，能先把问题定位到是链路还是模型。

**Q：测试必须分进程跑吗？**
必须。每个服务都有顶层 `app` 包，同一个 pytest 会话里 `app` 只会绑定到最先导入的那个服务。用 `python scripts/test_all.py`。

---

## 贡献指南

见 [`CONTRIBUTING.md`](CONTRIBUTING.md)。要点：中文三引号 docstring，写「为什么」与契约而非翻译代码，涉及鉴权/越权/限流/审计处显式标注 fail-closed / fail-open 的失败走向。

---

## 许可证

[MIT](LICENSE)。Copyright (c) 2026 弄弄nongnong。