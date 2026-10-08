# permission-aware-rag

企业级**权限感知** RAG 知识库平台：把「接入文档 → 切分 → 向量化 → 混合检索 → 大模型生成 → 带引用作答」端到端跑通，并把权限过滤、异步通道、评测接缝预留在正确的位置。

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
# 需要 Kafka / Keycloak+OPA / Langfuse / 监控时：
docker compose --profile streaming --profile authz --profile observability --profile monitoring up -d
```

> 本机 6379/19530 等端口被占用时，改 `.env` 里的 `REDIS_PORT` / `MILVUS_URI`（compose 按 `.env` 插值）。

### 2.3 安装与建表

```bash
python -m venv .venv && .venv/Scripts/activate    # Windows；Linux 用 source .venv/bin/activate
pip install -e ".[dev]"
python scripts/migrate.py                         # Alembic 建表
```

### 2.4 配置模型（DeepSeek 双模）

```ini
# .env —— 官方 API（默认路径）
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=sk-xxxx

# .env —— 本地 OpenAI 兼容端点（vLLM / 通用 OpenAI 协议服务）
LLM_PROVIDER=local
LOCAL_LLM_BASE_URL=http://localhost:11434/v1
LOCAL_LLM_MODEL=deepseek-r1:7b
LOCAL_LLM_API_STYLE=openai

# .env —— 本机 Ollama（推荐本地开发用它）
LLM_PROVIDER=local
LOCAL_LLM_MODEL=qwen3.5:4b
LOCAL_LLM_API_STYLE=ollama   # 走 Ollama 原生接口
LOCAL_LLM_THINK=false        # 关闭思考链，见下方说明
LLM_ANSWER_PROMPT_VERSION=v2
```

> **思考型模型必须关掉思考链**：`qwen3` / `deepseek-r1` 在 OpenAI 兼容接口下会把输出预算耗在思维链上，最终 `content` 为空，表现为「一直拒答」。只有 Ollama 原生接口支持 `think=false`，故提供 `LOCAL_LLM_API_STYLE=ollama`；非思考型模型保持 `openai` 即可。

向量化默认用本地 ONNX（`EMBED_BACKEND=fastembed` + `BAAI/bge-small-zh-v1.5`，512 维），无需外部服务；也可切 `litellm` 走远端 embedding 端点。

### 2.5 启动服务

```bash
python scripts/dev_services.py     # 一键前台启动 6 个在线服务（Ctrl+C 全部退出）
# 或按需单独启动
python -m uvicorn app.main:app --app-dir services/retrieval --port 8002 --reload
```

打开 <http://localhost:8000/ui/> 即为问答界面（零构建单页应用）：左侧会话历史，右侧对话区；侧栏可上传文档（`.md/.markdown/.txt/.pdf`，点击或拖拽，也可填服务器路径）与知识库台账（关键字搜索、重建索引、删除）。答案按 Markdown 渲染，`[n]` 变可点击角标，点击高亮并滚动到来源卡片；拒答渲染成中性提示而非报错。支持深色模式、复制、重新生成、停止等待。

> 当前为**服务端一次性返回 + 思考中提示**，不是逐 token 流式输出；真流式需三级 SSE 透传，见第 8 节。

### 2.6 导入语料并验收

```bash
python scripts/seed.py               # 导入 data/corpus（幂等）
python scripts/verify_loop.py        # 全量验收 V1~V5（需要可用的 LLM 配置）
python scripts/verify_loop.py --skip-chat   # 只验服务健康、接入幂等与引用回查（不需要 LLM）
```

| 判据 | 内容 |
| --- | --- |
| V1 | 6 个服务 `/health` 正常，Milvus / OpenSearch 真实连通 |
| V2 | 语料接入成功且**可重复执行**（两次 chunk 数一致） |
| V3 | **检索级引用回查**：`content[char_start:char_end] == hit.text` 逐字成立 |
| V4 | 正样本返回非空答案 + 非空 `citations`，且引用区间与原文一致 |
| V5 | 负样本（文档里没有的问题）走拒答，`refused=true` 且 `citations=[]` |

### 2.7 评测与门禁

评测分两层：**L1 用可判定的硬事实，不依赖裁判模型**；L2 用 RAGAS 打答案质量分。指标定义与取舍见 `docs/adr/0005`。

```bash
make corpus           # 准备语料（10 篇通用 + 3 篇权限，回读校验 ACL）
make eval-preflight   # 前置检查：语料是否入库、鉴权是否可用（秒级，不调用模型）
make eval-fast        # 只跑 L1（确定性指标，分钟级，适合改参数后反复跑）
make eval             # 全量（L1 + L2 RAGAS）
make eval-rescore     # 不重新采集，用上次落盘的采集结果重打分
make corpus-check     # 语料一致性：规范值冲突、引用断链、重复句、占位符（秒级，不依赖模型）
```

当前基线（70 条样本、`top_k=5`、`temperature=0`、作答与裁判均为 DeepSeek、提示词 v4）：

```
L1  hit@k 100.0%（95% 区间 92.3%~100.0%）  片段召回 98.0%    引用覆盖 100.0%
    MRR(引用序) 0.9891   MRR(检索侧) 0.9891   NDCG@5 0.9864（46 条样本）
    漏答率 0.0%   误答率 0.0%   越权泄露 0 条   禁用内容 0 条   延迟 P50 688 ms / P95 1065 ms
L2  faithfulness 0.979   context_precision 0.917   context_recall 0.958   （12 条分层抽样）
```

评测集含权限切片（同一问题、不同身份、期望相反）与注入回归，其判定是可复现的硬事实，**必须为 0**，不依赖裁判模型。

回归门禁：

```bash
make eval-gate        # 对比 configs/eval/baseline.json，指标不许变差
make eval-baseline    # 把当前报告冻结为新基线（显式接受现状，需 review）
```

`scripts/check_eval_regression.py` 分两类判定：

- **零容忍不变量**：`leak_count`（越权）、`forbidden_count`（注入得逞）必须为 0；字段缺失也算失败，否则旧版报告缺字段会被当成 0 而绕过门禁；
- **指标退化**：检索类容差 0（temperature=0 下确定：`hit@k` / `MRR`（引用序）/ `MRR`(检索侧) / `NDCG@5` / 片段召回 / 引用覆盖），拒答类容差 0.05，L2 容差 **0.10**。L2 容差取自实测：同一份答案、同一裁判连打 5 次，faithfulness 极差约 0.052。

门禁的三条边界行为：样本数不一致时跳过指标对比（分母不同、比率不可比）；基线仅在显式 `--update-baseline` 时变更；基线中不存在的指标其规则不生效，门禁会提示先跑一次 `--update-baseline`。

指标命名的两处易混淆处：`MRR(引用序)` 量生成侧（模型挑了哪些引用、按什么顺序排列），`MRR(检索侧)` / `NDCG@5` 量检索侧（RRF 融合与重排后的真实名次），两者不可直接比大小；`NDCG@5` 需与 `NDCG 样本数` 一起看，分母变化后不可比。评测固定传 `ChatRequest.use_cache=false`（生产默认不变），否则命中响应缺 contexts 会让检索侧分母随缓存命中浮动。详见 `docs/adr/0005`。

报告落盘 `eval_data/reports/`（`baseline_latest.json` 供机器比对、`baseline_latest.md` 供人读）。

### 2.8 权限闭环验收（S7，本项目的主线）

V1~V5 只覆盖单租户单部门；「权限感知」需用**真实身份**跑隔离矩阵：

```bash
docker compose --profile authz up -d   # Keycloak + OPA
make verify-permissions                # 清理历史产物 -> 上传权限语料 -> 跑隔离矩阵
python scripts/verify_permissions.py --skip-prepare   # 沿用现有语料，不改动知识库
```

判据全部用 doc_id 集合断言，不依赖模型措辞：

| 判据 | 内容 |
| --- | --- |
| P0 | 探针 `/health` 免鉴权可用；无令牌/伪造令牌访问 `/chat` 得 401；令牌声明被正确解析 |
| P1 | 按真实身份上传的文档，其 `tenant/department/visibility` **落库值与身份一致** |
| P2 | 4 个查询 × 5 个身份：每次回答的 `citations` 必须**完全落在该身份的可见集合内**；专属文档只有有权身份能命中 |
| P3 | **存储层过滤硬证据**：以 engineering 身份检索 HR 文档原句 → 结果 0 条 HR；同句以 hr 身份 → 命中（对照组成立） |
| P4 | OPA 生效：无写角色写入返回 403，有写角色放行；其他租户看不到 default 租户任何文档 |

语料与身份（`data/corpus_permissions/` + realm 内置账号）：

| 账号 | 租户 / 部门 | 角色 | 语料 |
| --- | --- | --- | --- |
| alice | default / hr | rag_user | 可见 hr_policy + 公共手册 |
| carol | default / hr | rag_user + **rag_writer** | 额外可见自己的 private 笔记 |
| bob | default / engineering | rag_user | 可见 eng_runbook + 公共手册 |
| erin | default / engineering | rag_user + **rag_writer** | 同 bob |
| dave | **tenant-b** / hr | rag_user | 租户隔离，一份都看不到 |

---

## 3. 目录结构

```
apps/           api-gateway（+ 预留 chat-ui / admin-console）
services/       query-orchestrator / retrieval / ingestion / indexing / model-gateway / eval / feedback / authz
packages/       共享库（contracts / common / llms / embeddings / search / vectorstores /
                retrievers / prompts / observability / security），不持有业务状态
pipelines/      离线任务：ingestion_dag（增量同步）· eval_dag（RAGAS 跑批）· cleanup_dag（留存/合规删除）
configs/        env / models(litellm.yaml) / prompts(版本化模板) / retrievers / tenants / corpus
infra/          docker（Dockerfile、Postgres/Keycloak 初始化）· k8s（helm + base/overlays）· terraform
                monitoring（prometheus.yml / alerts.yml / alertmanager.yml）
migrations/     postgres（Alembic）· opensearch（索引模板）
scripts/        migrate / dev_services / seed / verify_loop / verify_permissions / test_all
eval_data/      golden.jsonl（黄金评测集）
data/corpus/    演示语料
```

依赖方向铁律：`contracts → common → llms/embeddings/search/vectorstores/retrievers/prompts/security → services → apps`。服务之间只通过 `packages/contracts` 的契约通信，**不允许跨服务 import 实现细节**。

---

## 4. 关键设计约束

1. **权限过滤下沉到存储层**：过滤条件编译成 Milvus `expr` 与 OpenSearch `filter`，绝不在应用层裁剪 top_k 结果，否则越权文档会先挤占 top_k，导致有权限的文档检索不到。详见 `docs/adr/0002`。
2. **检索为空直接拒答，不调用 LLM**：检索为空仍编答案比不可用更危险。拒答判定分三层（哨兵标记 → 固定话术 → 短句否定表述），且**拒答一律清空 citations**。详见 `docs/adr/0003`。
3. **引用可定位**：`chunk` 自带 `char_start/char_end`，切分器保证 `content[char_start:char_end] == chunk.text`；`/chat` 的 `citations` 原样透传。
4. **混合检索用 RRF 而非分数加权**：BM25 与余弦相似度量纲不可比，RRF 只用排名，免标定。
5. **同步直连优先、Kafka 留接口**：`ChunkSink` 抽象让 ingestion→indexing 在「HTTP 直连 / Kafka 事件」间切换不改业务代码。详见 `docs/adr/0001`。
6. **可选能力默认关闭且显式可观测**：改写、重排、语义缓存默认关闭；关闭时是显式透传（响应如实返回 `reranker=rrf`），不伪装执行过。
7. **幂等优先**：`doc_id` 由 `source` 稳定派生（统一 posix 分隔符），Milvus 用 `chunk_id` 作主键、OpenSearch 用 `chunk_id` 作 `_id`，重跑索引不产生重复。
8. **权限数据的失败必须大声**：`tenant/department/owner` 只能由网关从身份注入，客户端不得指定；`visibility=private` 缺 `owner` 直接 422，不做默认值兜底。权限字段错误不抛异常，只会静默搜不到或静默越权。详见 `docs/adr/0004`。
9. **缓存键必须包含身份维度**：`(tenant_id, department_id, user_id, mode, top_k, temperature, query)`。缺身份维度会让同租户跨部门共用答案，检索层 ACL 被整段绕过，且该缺陷在 `CACHE_ENABLED=false` 时不可见。详见 `docs/adr/0006`。
10. **评测分两层**：L1 用可判定的硬事实（越权、拒答、命中），L2 才用 RAGAS 打答案质量分；只做 L2 会慢到没人愿意跑，且无法定位到样本。详见 `docs/adr/0005`。
11. **提示注入：声明 + 转义 + 检测告警，检测不阻断**：提示词把资料声明为**数据**，送模型前转义 `【参考资料】/【问题】/【回答要求】`（文档不能伪造分节），可疑表达只计数告警、不改变行为（用正则决定是否拒答会把可用性押在正则精确度上）。详见 `docs/adr/0007`。

---

## 5. 测试

```bash
python scripts/test_all.py            # 全部（按服务分进程）
python -m pytest packages/tests -q    # 只跑共享库
ruff check .                          # 静态检查
```

> 必须按服务分进程：每个服务都有顶层 `app` 包，同一 pytest 会话里 `app` 只会绑定到最先导入的服务。

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

## 7. 监控与告警

9 个服务全部暴露 `GET /metrics`（Prometheus 文本格式，实现见 `packages/observability/metrics.py`）。

| 指标 | 类型 | 标签 | 用途 |
| --- | --- | --- | --- |
| `http_requests_total` | counter | service / method / route / status | 可用性、错误率（含被拒请求） |
| `http_request_duration_seconds` | histogram | service / method / route | P95 / P99 |
| `http_requests_in_progress` | gauge | service | 拥塞判断 |
| `rag_answer_total` | counter | outcome=answered/refused/error | **拒答率与错误率** |
| `rag_cache_lookups_total` | counter | result=hit/miss/skip | 缓存命中率 |
| `rag_acl_missing_total` | counter | — | **权限下推链路断裂次数，生产必须恒为 0** |
| `rag_injection_suspected_total` | counter | source=query\|context / rule | 疑似提示注入次数（检测**不阻断**） |

两条约定：

- **标签取路由模板而非实际 URL**（`/documents/{doc_id}`），否则每个文档生成一条时间序列。匹配不到路由的归到 `<unmatched>`；`/metrics` 自身不计入。
- **指标中间件放在最外层**（starlette 越晚添加越靠外层）。401/429 与抛异常的请求都要计入，否则错误率会偏低。

`METRICS_TOKEN` 的两层行为：

- **全局生效**：一旦配置，所有服务的 `/metrics` 都要求 `Authorization: Bearer <token>`。因此 prometheus.yml 的**两个 job 都要带凭据**，只给网关带会让内部服务集体 403。
- **网关额外一层**：它的 `/metrics` 仅在配了 token 时才免用户鉴权（Prometheus 没有 Keycloak 令牌）；不配 token 则被身份中间件拦成 401。不配 token 时所有服务直接可达。

抓取配置与告警规则在 `infra/monitoring/`。`RagAclMissing`（`increase(rag_acl_missing_total[5m]) > 0`，`for: 0m`）对应绝对不变量：越权或权限下推断裂出现一次即告警。SLO 与阈值见 `docs/slo.md`。

> 仓库不附带 docker-compose 的 Prometheus 服务：本地应用服务由 `scripts/dev_services.py` 绑定 `127.0.0.1` 启动，容器内无法访问宿主 loopback。

---

## 8. 当前边界（明确未完成）

| 项 | 现状 | 后续 |
| --- | --- | --- |
| Keycloak / OPA | 代码与策略就绪，默认 `AUTHZ_ENABLED=false` 走固定身份 | 打开开关即启用，无需改代码 |
| Kafka | 仅占位（拓扑与接口已定），默认同步直连 | `USE_KAFKA=true` 切异步 |
| Langfuse | 未配置密钥时静默降级为 no-op | 配置 `LANGFUSE_*` 即开始上报 |
| 鉴权与授权 | 已接入 Keycloak（JWT + JWKS，验签失败自动刷新）与 OPA（默认拒绝白名单），端到端验收通过 | 字段级/文档级授权、令牌静默刷新、生产用 HTTPS + PKCE 回调域名 |
| 角色白名单可见性 | `allowed_roles` 已入契约但未参与过滤 | 补存储层 schema/expr 与两个入口的字段传递 |
| 语义缓存 | 当前为精确匹配 | 可升级为 embedding 相似度匹配（须同时保证身份隔离） |
| 查询规划 | 已实现（plan 节点 + 启发式闸门 + 轮转交织合并，`DECOMPOSE_ENABLED`）；**默认关闭** | 三版迭代实测无净增益（RRF 全量融合使多面向问题退化、分段配额被 top-k 截断、轮转交织持平但 NDCG 略降且多一次 LLM 调用） |
| Reranker | 已实现并接线（bge-reranker-base）；**默认关闭，两轮实测维持关闭** | 在有余量的 70 条难集上开启后质量四项无变化，P50/P95 852/1070 → 10782/22214 ms（CPU）。翻案条件：更强 reranker/GPU |
| 标准 Recall@K | 仍以 `hit@k` 代替（比分母意义上的 Recall 宽松） | 声明多个期望来源并出现「部分命中」样本后，改为按期望来源计的召回率 |
| 中文分词 | OpenSearch 用 `standard` 分析器 | 换带 IK 插件的镜像并重建索引 |
| PDF / Word / HTML | 已支持 Markdown / Txt / PDF（pypdf，无 OCR） | 补 docx / html / OCR |
| 前端 | `api-gateway` 内置单页应用（问答 + 上传 + 知识库台账 + 深色模式） | `apps/chat-ui`、`apps/admin-console`（Next.js：评测看板、批量导入、权限配置） |
| 文档管理 | 列表 / 关键字搜索 / 重建索引 / 删除（`GET/DELETE /documents`） | 批量上传任务化、版本历史、失败重试 |
| 批量上传 | 前端串行逐个上传 | 改走 Kafka 异步通道（`USE_KAFKA=true` 时 ingestion 已支持） |
| 流式输出 | 一次性返回 + 思考中提示 | model-gateway → orchestrator → gateway 三级 SSE 透传（LangGraph `astream` 已可提供节点级进度） |
| 拒答的兜底判定 | 哨兵 + 固定话术 + 短句启发式（阈值 80 字） | 用评测集标定相关性阈值，让不可回答的问题在检索阶段返回空 |
| 入库吞吐 | 单文档 `/index` 因 Milvus `flush` + OpenSearch `refresh` 约 20s（本机实测） | 大文档改批量写入 + 关闭同步 refresh，用 bulk 参数控制可见性 |
| 告警通道 | ✅ 已接线：规则 + `alertmanager.yml`（critical/warning 分路）+ compose `monitoring` profile（`scripts/dev_monitoring.ps1` 一键起）。webhook 接收器为占位符 | 换成钉钉/企微/Slack；补 Grafana 看板 |
| 监控看板 | 无 Grafana 看板 | 按第 7 节指标表建四块面板：P95 / 错误率 / 拒答率 / 缓存命中率 |
| SLO 阈值 | ✅ 已成文（`docs/slo.md`）：每条 SLO 注明度量手段与对应告警，并列出不设 SLO 的项及理由 | 按月复核 SLO 与实测差距，调整阈值 |
| 服务间身份信任 | 编排与检索从明文 header 取身份（网关是唯一鉴权点）——已 fail-closed：缺头即 403 | mTLS 或服务网格；当前不可达（应用服务不发布端口 + NetworkPolicy） |
| 提示注入 | 三层已实现（声明 + 结构转义 + 检测告警），含 2 条常驻回归样本 + 零容忍门禁。残余：检测只覆盖已知表达形式，转义只覆盖当前模板用的三个标记 | 模板改动时同步 `_FORGEABLE_SECTIONS`；`data/corpus/injection_probe.md` 是**故意投毒**的夹具，勿当垃圾清理 |
| 语料一致性 | 18 条规范值规则 + 引用断链 + 无规则守护的重复句，接入 CI（`make corpus-check`）。规则声明在 `configs/corpus/normative_facts.yaml` | 规则覆盖范围仍为人工挑选；没有规则守着的事实仍可能互相矛盾，补规则是持续动作 |
| 上传内容校验 | 只做大小与非空校验，无 MIME / 内容类型校验、无投毒检测 | 加类型白名单与内容扫描；投毒靠 `RagPromptInjectionInContext` 告警兜住 |
| 数据失效管理 | ✅ 已实现：声明式生命周期（`configs/corpus/lifecycle.yaml`）+ 库层过滤（`must_not`，见 `compile_filters`），废止文档不参与检索且台账可见。残余：日期判定在入库时，跨失效日需重新入库 | 自动翻转可改为把生效/失效日期建成可比较字段并在查询时注入「今天」；多版本并存与「指向新版」的答案提示 |
| 成本核算 | ✅ 已折算：`rag_llm_cost_usd_total{model,tenant}` 按模型单价计美元（`LLM_PRICES` 可配），按租户+日累计进 Redis，`GET /admin/quotas/{tenant}` 返回 `cost_today_usd`。单价会漂移，以供应商账单为准校准 | 对接账单系统；按成本维度做配额 |
| 审计日志 | ✅ 已落库：网关访问审计进 Postgres `audit_logs`（谁/何时/访问什么/结果），`GET /admin/audit` 可按租户与用户检索（需 rag_admin）。队列+批量写，失败丢弃并告警日志，不影响业务请求 | 审计行加保留期与自动归档；记录「访问了哪些文档」需在编排层补 |

> 实测（本机 Docker + CPU 推理）：`/chat` 端到端约 2–14s，其中检索 ~0.3s、生成 2.3–12.6s；`/index` 单文档（10 分块）约 20s，瓶颈在 Milvus flush 与 OpenSearch refresh，不在向量化。

数据失效管理的落地要点：新增 `lifecycle` 字段需**重建 Milvus 集合**（schema 无 alter，`enable_dynamic_field=False`，存量集合不会自动补列），用 `python scripts/rebuild_index.py`；OpenSearch 侧不用重建，`ensure_index` 会幂等 `put_mapping` 补齐（否则 dynamic mapping 把字符串推断成 text 并分词，term 过滤匹配不上，属静默失效）。另需注意**问已废止标准时系统用现行标准回答而不是拒答**（废止文档不进上下文、引用指向现行制度），是否显式提示「该版本已废止」是产品决策，待固化。

历史 P0 设计与坑位清单见 `docs/architecture/minimal-loop-v0.md`。

---

## 9. 代码风格与注释约定

完整规范见 [`CONTRIBUTING.md`](CONTRIBUTING.md)。要点：

- 中文三引号 docstring，放在模块、类、公共函数首行；
- 写「为什么」与「契约 / 语义」，不翻译代码；
- 涉及鉴权、越权、限流、审计处，显式标注 fail-closed / fail-open 的失败走向。

测试文件不强制，但鼓励同风格。