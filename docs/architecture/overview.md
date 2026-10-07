# 架构总览

本文说明**数据契约、调用链与生命周期**三件事；部署与本地启动见根目录 `README.md`，
关键取舍见 `docs/adr/`。

---

## 1. 数据契约（单一事实来源）

所有跨服务请求/响应都在 `packages/contracts/schemas.py` 定义，服务之间**只能**通过这些类型交互。
核心对象：

| 契约 | 关键字段 | 为什么这些字段必须存在 |
| --- | --- | --- |
| `ACL` | `tenant_id / department_id / visibility / owner` | 检索侧权限过滤的唯一依据；建表时就必须写进向量库 schema |
| `Chunk` | `chunk_id / doc_id / text / chunk_index / char_start / char_end / section_path / acl` | `char_start/char_end` 是「引用可精确定位原文」的硬前提 |
| `SearchHit` | 上述 + `score / retriever` | `retriever` 记录命中来源（vector/bm25/hybrid/rerank），便于归因 |
| `ContextItem` | `index / chunk_id / text / ...` | `index` 即引用编号；context 顺序 == prompt 编号顺序 == citations 顺序 |
| `Citation` | `index / chunk_id / doc_id / chunk_index / char_start / char_end / snippet` | 前端「点击跳原文」所需信息一次给全，避免回头补偏移量 |
| `IndexRequest` | `chunks / reindex` | `reindex=true` 时先按 doc_id 清理旧索引，避免文档变短后残留旧 chunk |

**引用编号的一致性**：`app/prompts/context.py::build_context_items` 是编号的唯一来源，
`guard.build_citations` 从**同一列表**派生引用。任何一处重新排序都会导致引用张冠李戴。

---

## 2. 主链路（在线问答）

```
POST /chat
 └─ api-gateway
     ├─ IdentityMiddleware：JWT → Identity（AUTHZ_ENABLED=false 时用固定身份）
     ├─ RateLimitMiddleware：Redis 固定窗口（fail-open）
     ├─ AuditMiddleware：记录 谁/何时/访问了什么/结果（不记正文）
     └─ 转发 → query-orchestrator（身份经 X-Tenant-Id 等请求头下传）

 query-orchestrator（LangGraph）
   cache_lookup ──命中──▶ END
        │未命中
   rewrite   查询改写（默认关闭=透传；开启走 model-gateway /complete）
   route     决定检索模式 + 生成 ACL（身份 → ACL）
   retrieve  调用 retrieval /search（召回 candidate_k 条）
        │空
        └──▶ refuse（返回固定拒答话术，**不调用 LLM**）──▶ END
   rerank    可选重排 + 构造 contexts（引用编号在此诞生）
   generate  调用 model-gateway /generate（送**用户原问题**，不是改写后的查询）
   guard     引用映射 + 拒答识别 + 可选 LLM 合规审核
   cache_store 回填缓存（拒答结果不缓存）
```

要点：

- **编排层不直连任何存储**，只通过 HTTP 调用 retrieval 与 model-gateway，因此它的依赖面只有两跳。
- **检索为空 → 拒答且不调用 LLM**：省一次调用，并从源头堵死幻觉。
- **生成用原问题**：改写只服务于检索，把改写结果当问题会让答案答非所问。

---

## 3. 接入链路（离线写入）

```
POST /documents/upload（或 /documents/ingest，path 指向文件/目录）
 └─ ingestion
     ├─ 解析：parsers/{markdown,text,pdf}（注册表按扩展名路由）
     ├─ 切分：chunkers/recursive.py
     │    1) 扫 Markdown 标题 → 标题栈 → 章节区间 + 层级路径
     │    2) 章节正文按分隔符递归细分 → 原子 → 贪心装箱（带重叠）
     │    3) 保证 content[char_start:char_end] == chunk.text
     ├─ Postgres：documents(status=pending) + chunks 明细
     ├─ ChunkSink → indexing /index
     └─ 成功后把 status 改为 indexed（失败置 failed）
```

`indexing` 做两件事并保持幂等：embedding（本地 ONNX 或远端）→ 双写
Milvus（主键 `chunk_id`，COSINE + HNSW，写后 `flush`）与 OpenSearch（`_id = chunk_id`）。

**顺序不可颠倒**：元数据先写、索引后写、最后才置状态；删除时反过来（先索引后元数据）。

---

## 4. 生命周期与状态

| 对象 | 存储 | 状态/生命周期 |
| --- | --- | --- |
| 文档台账 | Postgres `documents` | `pending → indexed / failed`；`content_hash` 用于增量同步判据 |
| 分块明细 | Postgres `chunks` | 随文档级联删除 |
| 检索索引 | Milvus + OpenSearch | 按 `doc_id` 清理；`reindex=true` 时先清后写 |
| 接入任务 | Postgres `ingestion_jobs` | `running → succeeded / failed`，`/jobs/{id}` 可查 |
| 查询缓存 | Redis | 精确匹配，默认关闭；拒答不入缓存（补录文档后不应继续吐旧答案） |
| 反馈 / 坏例 | Postgres `feedback` | 点踩记录导出为 `eval_data/bad_cases/*.jsonl`，成为回归集种子 |

---

## 5. 可观测性

- 每个服务 `/health` 返回**真实连通性**：retrieval 检查 Milvus 与 OpenSearch 是否可用并报告行数，
  ingestion 检查 Postgres 与 indexing 可达性；依赖不可达时状态为 `degraded` 而不是 `error`
  （进程健康 vs 依赖缺失，决定了告警该打给谁）。
- `/chat` 的 `timings_ms` 由各节点自报累加（`retrieve` / `generate` / `total` 等），
  其中 `generate` 是网关侧模型耗时，`generate.round_trip` 是含网络的往返耗时——两者分开才能区分
  「模型慢」与「网络慢」。
- Langfuse 未配置时**完全静默**（no-op，无网络开销）；上报内容中的正文统一截断到 200 字符。

---

## 6. 阶段边界

当前实现的是**单租户最小闭环**：权限与租户字段已贯穿契约、索引 schema 与过滤编译器，
但默认使用固定身份、`AUTHZ_ENABLED=false`。也就是说，打开鉴权是**配置变更**，不是代码改造：

1. `AUTHZ_ENABLED=true` + Keycloak realm 导入（`infra/docker/keycloak/realm-rag.json` 已含租户/部门 claim 映射）；
2. 网关的 `require_action` 会开始调用 OPA，策略见 `services/authz/policies/rag.rego`（默认拒绝）；
3. retrieval 的过滤已按 ACL 下推，无需改动。

历史 P0 设计与坑位清单见 `minimal-loop-v0.md`。
