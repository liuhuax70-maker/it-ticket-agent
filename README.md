# permission-aware-rag

企业级**权限感知** RAG 知识库平台。当前阶段目标：把「接入文档 → 切分 → 向量化 → 混合检索 → 大模型生成 → 带引用作答」这条链路端到端跑通，并把权限过滤、异步通道、评测等接缝**预留在正确的位置**，避免后续返工。

技术栈：`FastAPI · LangGraph · Postgres · OpenSearch · Milvus · Redis · Kafka · LiteLLM · RAGAS · Langfuse · Keycloak/OPA · K8s · Terraform`
语言模型：**DeepSeek**（官方 API 与本地 OpenAI 兼容端点**双模可切换**）

---

## 1. 架构与数据流

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

---

## 2. 快速开始（本地）

### 2.1 环境要求

Python 3.11+、Docker / Docker Compose。可选：本机 Ollama（无 DeepSeek 密钥时用它跑通闭环）。

### 2.2 启动依赖

```bash
cp .env.example .env          # 按需修改端口与模型配置
docker compose up -d          # 核心依赖：Postgres / Redis / OpenSearch / Milvus(+etcd+MinIO)
# 需要 Kafka / Keycloak+OPA / Langfuse 时：
docker compose --profile streaming --profile authz --profile observability up -d
```

> 本机 6379/19530 等端口被占用时，改 `.env` 里的 `REDIS_PORT` / `MILVUS_URI` 即可（compose 会按 `.env` 插值）。

### 2.3 安装与建表

```bash
python -m venv .venv && .venv/Scripts/activate    # Windows；Linux 用 source .venv/bin/activate
pip install -e ".[dev]"
python scripts/migrate.py                         # Alembic 建表
```

### 2.4 配置模型（DeepSeek 双模）

```ini
# .env —— 官方 API
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=sk-xxxx

# .env —— 本地 OpenAI 兼容端点（Ollama / vLLM）
LLM_PROVIDER=local
LOCAL_LLM_BASE_URL=http://localhost:11434/v1
LOCAL_LLM_MODEL=deepseek-r1:7b
```

向量化默认用本地 ONNX（`EMBED_BACKEND=fastembed` + `BAAI/bge-small-zh-v1.5`，512 维），无需外部服务；也可切 `litellm` 走远端 embedding 端点。

### 2.5 启动服务

```bash
python scripts/dev_services.py     # 一键前台启动 6 个在线服务（Ctrl+C 全部退出）
# 或按需单独启动
python -m uvicorn app.main:app --app-dir services/retrieval --port 8002 --reload
```

打开 <http://localhost:8000/ui/> 即为最小问答页。

### 2.6 导入语料并验收

```bash
python scripts/seed.py               # 导入 data/corpus（幂等）
python scripts/verify_loop.py        # 全量验收 V1~V5（需要可用的 LLM 配置）
python scripts/verify_loop.py --skip-chat   # 只验服务健康、接入幂等与引用回查（不需要 LLM）
```

验收判据：

| 判据 | 内容 |
| --- | --- |
| V1 | 6 个服务 `/health` 正常，Milvus / OpenSearch 真实连通 |
| V2 | 语料接入成功且**可重复执行**（两次 chunk 数一致） |
| V3 | **检索级引用回查**：`content[char_start:char_end] == hit.text` 逐字成立 |
| V4 | 正样本返回非空答案 + 非空 `citations`，且引用区间与原文一致 |
| V5 | 负样本（文档里没有的问题）走拒答，`refused=true` 且 `citations=[]` |

---

## 3. 目录结构

```
apps/           api-gateway（+ 预留 chat-ui / admin-console）
services/       query-orchestrator / retrieval / ingestion / indexing / model-gateway / eval / feedback / authz
packages/       共享库（contracts / common / llms / embeddings / search / vectorstores /
                retrievers / prompts / observability / security），不持有业务状态
pipelines/      离线任务：ingestion_dag（增量同步）· eval_dag（RAGAS 跑批）· cleanup_dag（留存/合规删除）
configs/        env / models(litellm.yaml) / prompts(版本化模板) / retrievers / tenants
infra/          docker（Dockerfile、Postgres/Keycloak 初始化）· k8s（helm + base/overlays）· terraform
migrations/     postgres（Alembic）· opensearch（索引模板）
scripts/        migrate / dev_services / seed / verify_loop / test_all
eval_data/      golden.jsonl（黄金评测集）
data/corpus/    演示语料
```

依赖方向铁律：`contracts → common → llms/embeddings/search/vectorstores/retrievers/prompts/security → services → apps`。
服务之间只通过 `packages/contracts` 的契约通信，**不允许跨服务 import 实现细节**。

---

## 4. 关键设计决策

1. **权限过滤下沉到存储层**：过滤条件编译成 Milvus `expr` 与 OpenSearch `filter`，绝不在应用层裁剪 top_k 结果——否则越权文档会先挤占 top_k，导致有权限的文档检索不到。详见 `docs/adr/0002`。
2. **检索为空直接拒答，不调用 LLM**：一个在检索为空时仍然编答案的 RAG 接口，比不可用更危险。
3. **引用可定位**：`chunk` 自带 `char_start/char_end`，切分器保证 `content[char_start:char_end] == chunk.text`；`/chat` 的 `citations` 原样透传，前端可直接跳原文。
4. **混合检索用 RRF 而非分数加权**：BM25 与余弦相似度量纲不可比，RRF 只用排名，免标定。
5. **最小闭环先同步直连、Kafka 留接口**：`ChunkSink` 抽象让 ingestion→indexing 在「HTTP 直连 / Kafka 事件」之间切换时不用改业务代码。详见 `docs/adr/0001`。
6. **可选能力默认关闭且显式可观测**：改写、重排、语义缓存默认关闭；关闭时是**显式的透传**（响应里如实返回 `reranker=rrf`），而不是假装做过。
7. **幂等优先**：`doc_id` 由 `source` 稳定派生（统一 posix 分隔符），Milvus 用 `chunk_id` 作主键、OpenSearch 用 `chunk_id` 作 `_id`，重跑索引不产生重复。

---

## 5. 测试

```bash
python scripts/test_all.py            # 全部（按服务分进程；服务各自有名为 app 的包，必须隔离）
python -m pytest packages/tests -q    # 只跑共享库
ruff check .                          # 静态检查
```

> 为什么按服务分进程：每个服务都有顶层 `app` 包，同一个 pytest 会话里 `app` 只会绑定到最先导入的那个服务。

---

## 6. 部署

```bash
make image                                   # 构建统一服务镜像
helm template api-gateway infra/k8s/helm/rag-service -f infra/k8s/helm/values/api-gateway.yaml
kubectl kustomize infra/k8s/base             # 集群级共享资源（ConfigMap/NetworkPolicy/PDB）
terraform -chdir=infra/terraform/envs/dev init && terraform -chdir=infra/terraform/envs/dev plan
```

职责划分：**Terraform** 管依赖组件（PG/Redis/Milvus/OpenSearch/Kafka/Keycloak/OPA），**Helm** 管业务服务工作负载，**Kustomize** 管集群级共享资源与环境差异。

---

## 7. 当前边界（明确未完成）

| 项 | 现状 | 后续 |
| --- | --- | --- |
| Keycloak / OPA | 代码与策略就绪，默认 `AUTHZ_ENABLED=false` 走固定身份 | 打开开关即启用，无需改代码 |
| Kafka | 仅占位（拓扑与接口已定），默认同步直连 | `USE_KAFKA=true` 切异步 |
| Langfuse | 未配置密钥时静默降级为 no-op | 配置 `LANGFUSE_*` 即开始上报 |
| 语义缓存 | 当前为精确匹配 | 建立 eval set 后升级为 embedding 相似度匹配 |
| Reranker | 默认关闭（透传） | 有 eval set 后再开，否则无法归因 |
| 中文分词 | OpenSearch 用 `standard` 分析器 | 换带 IK 插件的镜像并重建索引 |
| PDF / Word / HTML | 已支持 Markdown / Txt / PDF（pypdf，无 OCR） | 补 docx / html / OCR |
| 前端 | 仅 `api-gateway` 内置静态问答页 | `apps/chat-ui`、`apps/admin-console`（Next.js） |

历史 P0 设计与坑位清单见 `docs/architecture/minimal-loop-v0.md`。
