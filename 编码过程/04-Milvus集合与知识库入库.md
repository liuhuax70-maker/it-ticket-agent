# 04-Milvus 集合与知识库入库

> 加入日期：2026-09-30
> 关联设计：`开发流程/04-检索与编排设计.md` §2.2 / §2.3、`开发流程/06-测试与验收方案.md` §5.2
> 新增文件：`app/retrieval/milvus_store.py`、`app/retrieval/dense.py`、`app/retrieval/sparse.py`、`ingestion/ingest.py`、`knowledge_base/*`、`tests/test_milvus_store.py`
> 改动文件：`app/api/health.py`（修 IPv6 探测 bug）、`app/core/config.py`、`.env.example`

---

## 1. 在整体布局中的位置（从底层到总体）

本次打通的是**「知识库 → 向量库」的入库链路**，并顺手把两条检索通道从占位变为可用：

```
第 6 层  部署
第 5 层  接入        app/api/health.py      ← 依赖探测（本次修 bug）
第 4 层  编排        app/graph/            （待实现，将调用检索层）
第 3 层  检索能力
        ├── app/retrieval/milvus_store.py  ← 本次：客户端 + Schema + 索引 + 过滤/转换
        ├── app/retrieval/dense.py         ← 本次：embed() + search_dense()
        └── app/retrieval/sparse.py        ← 本次：search_sparse()（BM25）
        ingestion/ingest.py                ← 本次：读文档 → 切分 → 向量化 → 入库
        ingestion/chunk.py                    （上一篇已完成）
第 2 层  契约与配置   app/core/config.py    ← 本次：模型名、维度、地址
第 1 层  运行时       Milvus v3.0.2 + 宿主机 Ollama
```

- **依赖谁**：`milvus_store` 依赖 `core.config` 与 `schemas`；`dense` 依赖 `milvus_store` + Ollama；`ingest` 依赖 `dense` + `milvus_store` + `chunk`。
- **被谁依赖**：`app/retrieval/hybrid.py`（下一步的 RRF 融合）将同时调用 `search_dense` 与 `search_sparse`。
- **为什么要有它**：这是「检索能不能召回」的物理基础。没有它，后面的 RRF、重排、编排都无从谈起。

**技术栈：** pymilvus 3.0.2 · Milvus v3.0.2（HNSW + BM25 倒排 + INVERTED 标量索引）· Ollama `qwen3-embedding:0.6b`（1024 维）· httpx · Python argparse

---

## 2. 加入后的项目整体布局

```
RAG/
├── app/
│   └── retrieval/
│       ├── milvus_store.py     # ← 新增：客户端 / Schema / 索引 / 过滤 / 结果转换
│       ├── dense.py            # ← 实现：embed / embed_batched / search_dense
│       ├── sparse.py           # ← 实现：search_sparse（BM25）
│       ├── fuse.py             #    RRF（已完成）
│       ├── rerank.py           #    （待实现）
│       └── hybrid.py           #    （待实现，将组合 dense+sparse+fuse）
├── ingestion/
│   ├── chunk.py                #    切分（已完成）
│   └── ingest.py               # ← 实现：入库脚本
├── knowledge_base/             # ← 新增：示例知识库
│   ├── manual/login-module.md      （产品手册）
│   ├── faq/common-questions.md     （FAQ）
│   └── tickets/history.jsonl       （历史工单）
└── tests/
    ├── test_fuse.py            # 4
    ├── test_chunk.py           # 16
    └── test_milvus_store.py    # ← 新增 11
```

---

## 3. 本次新增清单

| 名称 | 类型 | 作用 |
| --- | --- | --- |
| `get_client` | 函数 | Milvus 客户端单例（`@lru_cache`） |
| `build_schema` | 函数 | 10 字段 Schema（含 `enable_analyzer` 与 BM25 Function） |
| `build_index_params` | 函数 | HNSW / SPARSE_INVERTED(BM25) / INVERTED 索引 |
| `ensure_collection` | 函数 | 创建集合，支持 `recreate` 重建 |
| `collection_stats` | 函数 | 行数统计（验证用） |
| `build_filter_expr` | 函数 | 过滤条件 → Milvus 表达式 |
| `to_chunks` | 函数 | Milvus 结果 → `Chunk` 列表 |
| `embed` / `embed_batched` | 函数 | 调 Ollama `/api/embed` 生成向量 |
| `search_dense` | 函数 | 稠密通道检索（HNSW + COSINE） |
| `search_sparse` | 函数 | 稀疏通道检索（BM25 全文检索） |
| `load_markdown` / `load_tickets` / `load_knowledge_base` | 函数 | 三种来源的加载 |
| `to_row` / `ingest` / `main` | 函数 | 组装插入行、执行导入、CLI 入口 |
| `knowledge_base/*` | 数据 | 3 个示例文档（手册 / FAQ / 工单） |
| `tests/test_milvus_store.py` | 测试 | 11 个纯函数用例 |

---

## 4. 功能逻辑（按跳转解释）

### 4.1 集合创建（一次性）

**触发点**：`ensure_collection(recreate=True)` 或首次入库。

```
ensure_collection()
   ├─ has_collection? ──是且非 recreate──► 直接返回（幂等）
   ├─ 是且 recreate ──► drop_collection() → 继续
   ▼
create_collection(schema = build_schema(), index_params = build_index_params())
   ├─ Schema：10 个字段
   │     └─ content 字段 enable_analyzer=True + analyzer_params={"tokenizer": "jieba"}
   │           └─► 供 BM25 切词使用
   ├─ Function：BM25(name=bm25, in=[content], out=[sparse])
   │     └─► 插入时**不需要**提供 sparse，Milvus 自动生成
   └─ Index：dense(HNSW/COSINE) · sparse(SPARSE_INVERTED/BM25) · source/error_code(INVERTED)
```

**技术栈：** pymilvus `MilvusClient.create_schema` / `prepare_index_params` / `create_collection` · `FunctionType.BM25`

### 4.2 知识库加载与切分

**触发点**：`python -m ingestion.ingest`

```
遍历 knowledge_base/
   ├─ manual/*.md  ──► load_markdown()  ─┐
   ├─ faq/*.md     ──► load_markdown()  ─┼─► split_document()（上一篇）
   └─ tickets/*.jsonl ─► load_tickets() ─┘      └─► RawChunk 列表
                                                    └─ metadata["source"] = manual/faq/ticket
```

- 工单按「问题 + 答复」拼成一段文本，`doc_id` 用 `ticket_id`，`title` 取问题（便于展示与引用）。
- 来源标记写在 `RawChunk.metadata`，避免靠 `doc_id` 前缀猜（更稳）。

**技术栈：** Python `pathlib` · `json`（JSONL 逐行解析，失败行跳过并告警）

### 4.3 向量化与写入

**触发点**：`ingest()` 拿到全部片段后。

```
去重 dedupe_chunks()
   ▼
embed_batched(全部 content)      # 分批（默认 16 条/批）
   └─► httpx.post(OLLAMA/api/embed, {model, input})
         ├─ HTTP 错误 ──► 抛 AppError(2002)
         └─ 数量不符 ──► 抛 AppError(2002)   # 防止错位写入
   ▼
to_row(chunk, vector)            # 字段名常量对齐 Schema，超长截断并告警
   ▼
client.insert(collection, rows)
   ▼
collection_stats()               # 回报 loaded / inserted / row_count
```

- **稀疏向量无需提供**：`sparse` 由 BM25 Function 在插入时生成，这是 Milvus 3.x 的关键便利。
- 批量向量化 + 单次 `insert`，减少往返次数。

**技术栈：** httpx（连接复用由库内部处理）· Ollama `/api/embed` · pymilvus `insert` / `flush`

### 4.4 稠密检索

**触发点**：`search_dense(query, top_n, filters)`

```
query
 └─► embed([query]) → 1024 维向量
        └─► client.search(anns_field="dense", metric=COSINE, params={"ef": 64})
              └─► to_chunks(results, score_field="dense_score")
                    └─► list[Chunk]（含 rank / dense_score）
```

**技术栈：** Ollama embed · HNSW（COSINE）· pymilvus `search`

### 4.5 稀疏检索（BM25）

**触发点**：`search_sparse(query, top_n, filters)`

```
query（原始文本，不需要自己切词/构造稀疏向量）
 └─► client.search(data=[query], anns_field="sparse", metric=BM25)
       └─► Milvus 侧：jieba 切词 → BM25 打分（k1=1.2, b=0.75）
             └─► to_chunks(results, score_field="sparse_score")
```

- 与稠密检索的**关键差异**：`data` 传的是**文本**而不是向量，`anns_field` 指向稀疏字段。
- 这正是「错误码/版本号能精准命中」的原因。

**技术栈：** Milvus 内置 BM25 · jieba 分词器 · SPARSE_INVERTED_INDEX

### 4.6 过滤与结果转换（共享）

**触发点**：`build_filter_expr()` / `to_chunks()` 被两个通道共用。

```
RetrievalFilters(source, error_code)
   └─► build_filter_expr() → 'source == "manual" and error_code == "ERR-4041"'
                                 └─► 作为 search(filter=...) 传入（Milvus 标量过滤）

Milvus 结果 [[{id, distance, entity}]]
   └─► to_chunks() 取第一组
         ├─ distance → dense_score 或 sparse_score（由 score_field 决定）
         ├─ entity   → Chunk 各字段（source 反序列化为 DocSource 枚举）
         └─ 按顺序写 rank（1 起）
```

**技术栈：** Python（枚举反序列化、字段映射）

### 4.7 健康检查的 IPv6 修正（本次发现的 bug）

**触发点**：`GET /api/v1/health` 探测 Ollama 时。

```
修复前：asyncio.open_connection("localhost", 11434)
           └─► localhost 优先解析为 ::1 ──► Ollama 只监听 IPv4 ──► 一直挂起 ──► TimeoutError ──► 误报 down

修复后：loop.getaddrinfo(host, port)
           └─► 逐个地址尝试（::1 失败就试 127.0.0.1）
                 └─► 任一成功 ──► up
```

- 同时把配置里的地址改为 `127.0.0.1`（`MILVUS_HOST` / `OLLAMA_BASE_URL`），双保险。

**技术栈：** asyncio · `socket.getaddrinfo` · 简化版 happy-eyeballs

---

## 5. 关键实现说明

| 项 | 说明 |
| --- | --- |
| **字段名用常量** | `FIELD_*` 常量统一在 `milvus_store.py`，避免各处拼字符串导致不一致 |
| **BM25 无需手动稀疏向量** | Schema 里注册 `Function(BM25)` 后，插入不传 `sparse`，Milvus 自动生成 |
| **jieba 分析器** | `analyzer_params={"tokenizer": "jieba"}`，实测创建成功（Milvus 3.0.2 支持） |
| **索引参数** | `bm25_k1=1.2`、`bm25_b=0.75`；HNSW `M=16`、`efConstruction=200` |
| **VARCHAR 长度保护** | Milvus 超长会直接插入失败，`_truncate()` 截断并告警（content 8000 / title 200） |
| **空值策略** | 未开 `nullable`，可选字段统一写 `""` 而非 `None` |
| **schema 长度校验** | `embed()` 校验返回向量数量与输入一致，防止错位写入 |
| **`flush()` 后才准** | `get_collection_stats` 在写入后需 `flush` 才能反映行数（首次查为 0 属正常） |

---

## 6. 与其他模块的衔接

| 衔接点 | 契约 | 状态 |
| --- | --- | --- |
| `hybrid.py` → `dense.search_dense` / `sparse.search_sparse` | `(query, top_n, filters) → list[Chunk]` | 🚧 下一步 |
| `hybrid.py` → `fuse.reciprocal_rank_fusion` | `list[list[Chunk]] → list[Chunk]` | ✅ 已就绪 |
| `rerank.py` → Ollama `dengcao/Qwen3-Reranker-4B:Q5_K_M` | 模型已在本机 | 🚧 待实现 |
| `graph/nodes/retrieve.py` → `hybrid_search` | 见 `开发流程/04` §2.1 | 🚧 待实现 |
| `api/retrieval.py` → `hybrid_search` | 调试接口 §4.4 | 🚧 待实现 |

---

## 7. 验证方式

```bash
python -m ingestion.ingest --recreate      # 入库
pytest -q                                  # 单测
```

**本次实测结果：**

| 检查 | 结果 |
| --- | --- |
| 集合创建 | ✅ `kb_chunks`，jieba 分析器 + BM25 Function 均生效 |
| 索引 | ✅ `idx_dense` / `idx_sparse_bm25` / `idx_source` / `idx_error_code` |
| 入库量 | ✅ 手册 5 + FAQ 5 + 工单 5 = **15 个片段**，`insert_count=15` |
| 数据量 | ✅ `flush` 后 `row_count=15` |
| 单元测试 | ✅ **31 passed**（RRF 4 + 切分 16 + 集合层 11） |

**两条通道的实际召回（证明互补性）：**

```
DENSE  「登录一直失败，提示令牌过期」
   manual-login-module#2   cos=0.7467   校验通过后签发访问令牌，令牌有效期默认 8 小时…
   manual-login-module#4   cos=0.6764   若迁移失败，系统会自动回滚…
   faq-common-questions#4  cos=0.6517   另外，若账号已因连续失败被锁定…

SPARSE 「ERR-4041」
   T-1002#0                bm25=1.8777  err=ERR-4041
   manual-login-module#4   bm25=1.2088  err=ERR-4041
   manual-login-module#2   bm25=1.0308  err=ERR-4041

SPARSE 「v1.3.0 强制退出登录」
   T-1001#0                bm25=9.4407  ver=v1.3.0
   manual-login-module#0   bm25=5.6469  ver=v1.2.0
   faq-common-questions#4  bm25=4.3071  ver=v1.3.0

SPARSE 「验证邮件收不到」
   T-1005#0                bm25=9.9886
   faq-common-questions#0  bm25=4.7695
```

> 结论：**稠密通道**对语义化提问有效；**BM25 通道**对错误码/版本号精准命中——正是设计文档预期的互补分工。

---

## 8. 待办 / 注意

- [ ] **⚠️ 不要启动 compose 里的 ollama 服务**：本机 Ollama 是**原生安装**在 `D:\Agent\Ollama`，已占用 11434 端口；两个会冲突。compose 里的 `ollama` 服务仅在换机器/无原生 Ollama 时才用。
- [ ] **LLM 模型与设计不一致**：设计文档写 `Qwen2.5`，本机实际是 `qwen3.5:9b`（另有 `qwen3.5:4b`、`gemma4:26b`）。已在配置中改为 `qwen3.5:9b`，生成阶段按此实现。
- [ ] **`--recreate` 会清空数据**：目前是「追加式」`insert` 入库。Milvus 的 `insert` 对重复主键**不会覆盖**（需用 `upsert`），因此对同一文档重复执行会累积重复片段 —— 需在下一次改动中改为 `upsert` 或「先按 doc_id 删除再插入」，当前依赖 `--recreate` 规避。
- [ ] **embedding 维度硬编码在配置**：`EMBEDDING_DIM=1024` 需与所用 embedding 模型一致，换模型必须同步改，否则集合维不匹配。
- [ ] **未做增量更新**：每次都全量重跑；文档量大后需加内容哈希比对做增量。
- [ ] **知识库规模仍是最小集**：15 个片段只够冒烟，评估集与 RAGAS 需要更多样本。
- [ ] `hybrid.py` 的降级逻辑（Milvus 挂掉走 BM25 等）尚未实现。

---

*（本文件为编码过程第 04 篇，记录 Milvus 集合、知识库入库与两条检索通道的落地。）*
