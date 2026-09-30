# 10-按 RAG 要点文档做加固

> 加入日期：2026-09-30
> 触发来源：`RAG编码要点.md`（「最不能犯的错误」清单）
> 改动范围：切分层 / 索引层 / 检索层 / 拼装层 / 生成层 / 评估层
> 新增文件：`app/core/entities.py`、`app/generation/citations.py`、`evaluation/run_generation_eval.py`、`evaluation/run_param_sweep.py`、`evaluation/run_chunk_compare.py`、`tests/test_citations.py`、`tests/test_prompts.py`

---

## 1. 在整体布局中的位置（从底层到总体）

本次不是加新功能，而是**按「最不能犯的错误」清单给已有链路补上安全带** —— 横跨 6 层：

```
第 3 层  能力
  ├── ingestion/chunk.py        切分：标题层级路径自解释 + overlap 默认 0
  ├── app/retrieval/milvus_store.py  索引：embedding 模型断言 + upsert 幂等 + 按源删除
  ├── app/retrieval/hybrid.py   检索：查询期去重
  ├── app/generation/prompts.py 拼装：去重 + 显式预算 + 去人设
  └── app/generation/citations.py 生成：引用回链校验 + 拒答判定  ← 新增
第 2 层  契约与配置
  ├── app/core/entities.py      实体抽取（切分与回链校验共用）    ← 新增
  └── app/core/config.py        top_k=8 / channel_timeout=10s / context_max_chars
────────────── 横向 ──────────────
      evaluation/               分层评测：检索层 / 生成层 / 参数扫参   ← 强化
```

- **依赖谁**：既有检索链路 + 本地 Ollama。
- **被谁依赖**：无（加固不改变对外接口契约）。
- **为什么要有它**：要点文档第 2 节指出「最致命的 6 个错误里，有 4 个不报错」。
  本次修的就是这类**静默劣化**。

**技术栈：** Milvus upsert / description 元数据 · Pydantic · 正则实体抽取 · 分层评测脚本

---

## 2. 本次新增 / 改动清单

| 项 | 类型 | 对应要点文档 | 说明 |
| --- | --- | --- | --- |
| `heading_path` 标题层级路径 | 改动 | 3.1③ 自解释 | 块正文前缀 `【一级 > 二级 > 三级】`，并写入 Milvus 标量字段 |
| `DEFAULT_OVERLAP_TOKENS = 0` | 改动 | 3.1 overlap | 默认关闭重叠，理由见 §4.1 |
| `ensure_collection` 写 description | 改动 | 3.2① 换模型不重建 | 集合 schema 里记录 embedding 模型 + 维度 + schema 版本 |
| `verify_index_consistency` | 新增 | 3.2① | 不入库/不服务时不通过则直接报错 |
| `upsert_rows` / `delete_by_source` | 新增 | 3.2④⑥ | 幂等写入 + 支持按来源删除 |
| `content_hash` / `embedding_model` 字段 | 新增 | 3.2⑤ | 每个片段可追溯来源与模型 |
| `dedupe_chunks`（查询期） | 新增 | 3.3 去重 | 精确 + 近重复（前 120 字）双重去重 |
| `build_context` 去重 + 预算 | 改动 | 3.4②③ | 显式丢弃并记录日志，不交给模型静默截断 |
| `SYSTEM_PROMPT` 去人设 | 改动 | 3.5④ | 只留约束，并强化数字原样保留 |
| `verify_citations` / `is_refusal` | 新增 | 3.5① 引用回链 | 引用有效性 + 实体落地 + 拒答判定 |
| 负样本评估集（6 条） | 新增 | 3.6③ | 占评估集 23%（20 条正 + 6 条负） |
| `run_generation_eval` | 新增 | 3.6② | 生成层独立评测：拒答率 + 引用忠实率 |
| `run_param_sweep` / `run_chunk_compare` | 新增 | 1.3 常数可追溯 | top_k 扫参 + 切分常数离线对比 |
| 日志降噪 | 改动 | 3.7 可观测 | 抑制 httpx 等噪声库 |

---

## 3. 功能逻辑（按跳转解释）

### 3.1 切分：让每个块都「自解释」

**触发点**：`split_document()`

```
逐行扫描文档
   ├─ 命中标题 → 维护标题栈
   │    ├─ while 栈顶层级 >= 当前层级: 弹出（同级/更高级标题）
   │    └─ 压入 (层级, 标题) → 得到 current_path
   ├─ 空行 → flush
   └─ 普通行 → 入缓冲

flush()：
   路径 = buf_path（该块所属的完整标题链）
   content = f"【{' > '.join(路径)}】\n" + 原文        ← 自解释前缀
   RawChunk(heading_path=路径, title=路径[-1])
```

- **效果**：任意块被单独召回后都能看懂「我是谁」；路径同时进入 embedding 文本与 BM25 索引，增加检索信号。
- 实测**路径覆盖率 100%**（全部 10 个块都带路径）。

**技术栈：** Python 列表栈 · dataclass

### 3.2 索引：把「向量空间」写进集合

**触发点**：`ensure_collection()` / `ingest()`

```
建集合时：
   build_index_meta() = {schema_version:2, embedding_model, embedding_dim, analyzer}
      └─► MilvusClient.create_schema(description=json.dumps({kb_index_meta: meta}))
            └─► 存进集合 schema 的 description（读回验证过）

入库前（非 --recreate）：
   verify_index_consistency()
      ├─ 读回 description 里的 meta
      ├─ 逐项比对 embedding_model / embedding_dim / schema_version
      ├─ 一致 → 放行
      └─ 不一致 → 抛 RuntimeError，提示 `--recreate` 重建
```

- **为什么**：换 embedding 模型不重建索引 = 新旧向量在不同空间，**不报错、不崩溃、只是慢慢变差**。
  这是全文档里最隐蔽的一条，因此做成**硬闸门**而不是文档约定。

**技术栈：** Milvus collection description（元数据随 schema 持久化）

### 3.3 入库：upsert 幂等 + 按源删除

**触发点**：`python -m ingestion.ingest`

```
读文档 → 切分 → 去重 → 批量向量化
   └─► to_row()：写入 content_hash / heading_path / embedding_model / source …
         └─► upsert_rows()   # 主键 chunk_id，重复执行不产生重复片段
               └─► count_by_source()  # 回报各来源条数，便于验证幂等
```

- `--drop-source manual` 可在写入前清理某来源，满足「删文档和加文档一样重要」。

**技术栈：** pymilvus `upsert` / `delete(filter=...)`

### 3.4 检索：查询期去重

**触发点**：`hybrid_search()` 融合之后、重排之前

```
RRF 融合结果
   └─► dedupe_chunks()
         ├─ 精确键：content_hash（无则现算）
         └─ 近重复键：归一化后的前 120 字
               └─► 命中任一 → 丢弃（保排名靠前的）
   └─►（可选）重排
```

- 近重复片段（同一内容被多来源收录）会占满 Top-K，把其他相关内容挤出去。
- 调试信息里新增 `fused_before_dedupe`，可观测去重了多少。

**技术栈：** `hashlib` + 归一化前缀索引

### 3.5 拼装：去重 + 显式预算

**触发点**：`build_context()`（draft 节点与降级模板都会调用）

```
检索片段
   ├─ _dedupe()          精确 + 近重复去重
   └─ 逐条累加字符数
        ├─ used + len(block) <= context_max_chars → 收入上下文
        └─ 超出 → 丢弃并记入 dropped
              └─► logger.warning("上下文预算不足，已丢弃 N 个片段: [...]")
   每段格式：[来源: chunk_id]（source）
             【标题层级路径】
             正文
```

- 要点文档 3.4③ 指出：静默截断可能恰好截掉答案所在片段。因此这里**由我们显式丢弃并留痕**。

**技术栈：** 字符预算 + 结构化日志

### 3.6 生成：引用回链校验 + 拒答判定

**触发点**：`verify_citations(answer, chunks)` / `is_refusal(answer)`

```
verify_citations(answer, chunks)
   ├─ 抽取 [来源: chunk_id]
   │    ├─ 不在本次检索结果里 → invalid_citations（伪造引用）
   │    └─ 在 → 收集其原文
   ├─ 抽取 answer 中的版本号/错误码（app.core.entities）
   │    └─ 未在被引原文中出现 → ungrounded_entities（数字漂移）
   └─ ok = 无无效引用 且 无未落地实体 且（有引用 或 不强制引用）

is_refusal(answer)
   └─ 命中「无法确定 / 未找到 / 资料不足 …」→ 视为正确拒答
```

- 要点文档 3.5②：**「引用看起来有据可查但不支撑结论」比完全编造更危险**，因此专门做这一层校验。

**技术栈：** 正则 + 共享实体抽取（`app.core.entities`）

### 3.7 评估：分层 + 常数可追溯

**触发点**：`python -m evaluation.<脚本>`

```
run_retrieval_eval   检索层：Hit Rate / Recall / MRR（正样本）
                            + 负样本 Top-1 分数诊断（不参与指标）
run_generation_eval  生成层：引用回链正确率 / 有引用比例 / 回答率（正样本）
                            + 拒答率（负样本）
run_param_sweep      按 k ∈ {3,5,8,10,15,20} 扫 Recall 曲线 → 定 top_k
run_chunk_compare    离线对比 chunk_size × overlap → 定 overlap
```

- **分层是核心**：端到端掉了，先看检索层掉没掉 —— 检索层没掉就改 prompt，掉了就改切分/检索。

**技术栈：** 纯 Python 统计（生成层无需 LLM 裁判，秒级~分钟级可跑）

---

## 4. 关键实现说明

### 4.1 为什么把 overlap 默认改成 0（有数据）

`evaluation/run_chunk_compare.py` 实测（同一语料，仅改 overlap）：

| chunk_size | overlap | 块数 | 均值 token | 标准差 | **重复句占比** | 路径覆盖 |
| --- | --- | --- | --- | --- | --- | --- |
| 300 | 0 | 10 | 84.2 | 24.5 | **0.00%** | 100% |
| 300 | 50 | 10 | 112.3 | 28.4 | **40.00%** | 100% |
| 400 | 0 | 10 | 84.2 | 24.5 | **0.00%** | 100% |
| 400 | 50 | 10 | 112.3 | 28.4 | **40.00%** | 100% |
| 500 | 0 / 50 | 10 | 84.2 / 112.3 | 24.5 / 28.4 | 0.00% / 40.00% | 100% |

两条结论：

1. **overlap=50 让 40% 的句子在多个块中重复** —— 重复会强化模型「多处提到 = 更可信」的错误确信，
   且实测把 MRR 从 0.95 拖到 0.86（对比 §7）。故定 **overlap=0**。
2. **chunk_size 在 300/400/500 下结果完全相同** —— 说明当前语料**每节都小于 300 token**，
   该参数暂不敏感。这也是一条可追溯的结论：等语料/章节变长后需重扫。

### 4.2 top_k 取 8（有曲线）

`evaluation/run_param_sweep.py`（正样本 20 条）：

| k | Hit Rate | Recall | MRR |
| --- | --- | --- | --- |
| 3 | 100% | 85.00% | 0.9500 |
| 5 | 100% | 90.83% | 0.9500 |
| **8** | 100% | **95.83%** | 0.9500 |
| 10 | 100% | 95.83% | 0.9500 |
| 15 | 100% | 100.00% | 0.9500 |
| 20 | 100% | 100.00% | 0.9500 |

- 收益拐点：**k=8**（Recall 95.8% 后进入平台期）。再往上 Recall 增长有限，但上下文成本线性上升。
- 该值已写入 `app/core/config.py` 的注释与 `.env.example`，从此「为什么是 8」有据可答。

### 4.3 两个新踩的坑

**① `create_collection(description=...)` 不持久化**

```
写法 A：client.create_collection(..., description=json)   → describe_collection().description == ''  ❌
写法 B：MilvusClient.create_schema(..., description=json) → 读回 'HELLO_META'                        ✅
```
最初用了写法 A，导致一致性闸门把**刚建好的集合**判为「未记录元数据」而误报。
已改为写法 B，并加了单测 `test_build_schema_carries_index_meta_in_description` 守住。

**② 通道超时过紧造成静默降级**

```
实测日志：dense 通道超时（5.0s），该通道降级 → mode=sparse_only
```
本地 embedding 在模型冷启动/负载高时会超过 5s，于是稠密通道被**静默降级成纯 BM25**——
正是要点文档警告的「不报错、只是变差」。已将 `CHANNEL_TIMEOUT_SECONDS` 默认放宽到 **10s**。

---

## 5. 与其他模块的衔接

| 衔接点 | 变化 | 影响 |
| --- | --- | --- |
| `Chunk` 模型 | 新增 `heading_path` / `content_hash` | 检索结果可标注来源路径、可去重 |
| Milvus schema | v1 → **v2**（+3 字段） | **必须 `--recreate` 重建**，闸门会拦住不匹配 |
| `build_context` | 返回值仍是 str，内部加去重与预算 | 调用方无需改动 |
| `draft_node` | 不变（继续调 `generate` / `generate_stream`） | — |
| 评估脚本 | 新增 3 个；`run_retrieval_eval` 支持负样本 | 需重新记录基线 |

---

## 6. 验证方式

```bash
pytest -q                                          # 137 passed
python -m evaluation.run_chunk_compare             # 切分常数对比
python -m evaluation.run_param_sweep               # top_k 扫参
python -m ingestion.ingest --recreate              # 重建索引（schema v2）
python -m evaluation.run_retrieval_eval            # 检索层
python -m evaluation.run_generation_eval           # 生成层（含拒答率）
```

### 6.1 索引幂等与一致性闸门

```
第一次 --recreate 入库: upserted=15, by_source={manual:5, faq:5, ticket:5}
第二次 直接入库:        upserted=15, by_source={manual:5, faq:5, ticket:5}   ← 幂等成立，无重复
```

把 `EMBEDDING_MODEL` 改成别的值后：

```
ok: False
stored: {'schema_version': 2, 'embedding_model': 'qwen3-embedding:0.6b', 'embedding_dim': 1024, ...}
mismatches: ["embedding_model: 索引='qwen3-embedding:0.6b' 当前='some-other-embedding-model'"]
→ RuntimeError: …请用 `python -m ingestion.ingest --recreate` 重建索引。
```

### 6.2 检索层（Top-8，20 条正样本）

```
Hit Rate@8: 100.00%
Recall@8  : 95.83%
MRR          : 0.9500
  专有名词类 n=10  hit=100% recall=95.00% mrr=0.9500
  语义化类   n=10  hit=100% recall=96.67% mrr=0.9500

负样本诊断（6 条）
  返回非空结果比例: 100%   ← 恒为 100% 属正常：缺少相关性门槛
  Top-1 稠密分均值: 0.3108
  Top-1 稀疏分均值: 3.3482
```

> **横向对比**：加固前 MRR 0.8625（语义化类仅 0.775）→ 加固后 **0.9500**（语义化类 0.9500）。
> 主要来自「overlap 归零 + 查询期去重 + 标题路径前缀」。

### 6.3 生成层（4 正 + 6 负）

```
引用回链正确率: 100.00%   （目标 ≥ 85%）
有引用比例    : 100.00%
回答率        : 100.00%

负样本拒答率: 100.00%（6/6，目标 ≥ 90%）
  全部正确拒答 ✅
```

### 6.4 单元测试

```
137 passed（原 98 → +39）
新增：test_citations.py(21) · test_prompts.py(6) · 切分路径(4) · 混合去重(3) · 一致性断言(5)
```

---

## 7. 对照要点文档：仍存的缺口

| 要点文档要求 | 现状 | 说明 |
| --- | --- | --- |
| eval set **≥ 200 条** | ⚠️ 26 条 | 当前只够冒烟与回归，不足以支撑统计显著性 |
| 负样本占 20–30% | ✅ 6/26 = 23% | — |
| 检索层 / 生成层分开评测 | ✅ | 两个独立脚本 |
| **small-to-big（检索小、生成大）** | ❌ 未做 | 检索与生成仍用同一粒度 |
| 查询改写 / 多查询扩展 | ❌ 未做 | — |
| 元数据过滤（来源/时间/**权限**） | ⚠️ 仅支持 source/error_code | **权限过滤是 to-B 红线**，本项目内网 MVP 未做 |
| 多跳 / 迭代检索 | ❌ 未做 | — |
| embedding 结果缓存 | ❌ 未做 | 相同 query 重复走 embedding |
| 增量更新（按 source） | ⚠️ 有 `--drop-source`，但仍是全量扫描 | — |
| 成本计量 / 查询 trace 持久化 | ❌ 未做 | 调试信息只进内存状态 |
| LLM 裁判人工校准 | ❌ 未做 | RAGAS 六指标基线仍未落数字 |
| **P95 延迟** | ⚠️ 正样本约 85s（本地 9B） | 负样本仅 17s（输出短）；本地算力是瓶颈 |

---

## 8. 待办

- [ ] 评估集扩到 200 条以上（含更多负样本与汇总/多跳类）。
- [ ] 补 RAGAS 六指标基线数字（链路已修好，缺一次完整运行）。
- [ ] 实现 small-to-big：小块检索、按 `doc_id` 回溯更大上下文再送入生成。
- [ ] embedding 结果缓存（按内容哈希）。
- [ ] 查询 trace 落盘（query / 改写 / 召回 id 与分数 / prompt / 输出 / 耗时）。
- [ ] 正样本生成延迟优化：换 `qwen3.5:4b` 或加 GPU；当前 ~85s 不适合生产。
- [ ] 权限标签与检索强制过滤（若进入 to-B 场景，这是红线）。

---

*（本文件为编码过程第 10 篇，记录按 RAG 要点文档做的系统性加固与实测证据。）*
