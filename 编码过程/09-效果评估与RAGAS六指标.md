# 09-效果评估与 RAGAS 六指标

> 加入日期：2026-09-30
> 关联设计：`开发流程/06-测试与验收方案.md` §5.4、§6
> 新增文件：`evaluation/testset.jsonl`（20 条）、`evaluation/common.py`、`evaluation/ragas_compat.py`、`evaluation/run_retrieval_eval.py`、`evaluation/run_ragas.py`、`evaluation/dump_chunks.py`
> 改动文件：`.gitignore`（忽略 `evaluation/reports/`）

---

## 1. 在整体布局中的位置（从底层到总体）

评估层**横跨在所有层之上**：它不参与线上请求，而是站在外部去度量整条链路的效果。

```
第 6 层  部署
第 5 层  接入            app/api/*
第 4 层  编排            app/graph/*
第 3 层  能力            retrieval / generation / memory
第 2 层  契约与配置
第 1 层  运行时          Milvus + Ollama
──────────────────────────────────────────────
（横向）评估            evaluation/          ← 本次完成
                        ├── testset.jsonl          20 条标注样本
                        ├── common.py              加载评估集 + 跑「被测系统」
                        ├── run_retrieval_eval.py  检索质量（无需裁判）
                        ├── run_ragas.py           RAGAS 六指标（需 LLM 裁判）
                        ├── ragas_compat.py        指标装配 + 导入兼容
                        └── dump_chunks.py         导出知识块，便于维护期望命中
```

- **依赖谁**：`hybrid_search`（检索）+ `generate`（生成）+ `prompts`（同一套 Prompt）。
- **被谁依赖**：无（纯离线工具），但它是**版本回归的门槛**：改动检索/生成后跑一遍即可判断有没有退化。
- **为什么要有它**：没有量化就没有「提升」。前面所有优化（RRF、重排、切分策略）都需要一个可比较的基线。

**技术栈：** RAGAS 0.4.3 · LangChain（`ChatOllama` / `OllamaEmbeddings`）· pandas · JSONL 标注

---

## 2. 加入后的项目整体布局

```
evaluation/
├── __init__.py
├── testset.jsonl              # ← 20 条标注（10 专有名词 + 10 语义化）
├── common.py                  # ← 评估集加载 + 被测系统封装
├── ragas_compat.py            # ← 六指标装配 + ragas 导入兼容
├── run_retrieval_eval.py      # ← 检索质量评估（快，无需裁判）
├── run_ragas.py               # ← RAGAS 六指标
├── dump_chunks.py             # ← 知识块导出（维护评估集用）
└── reports/                   # 报告产物（已 gitignore）
```

---

## 3. 本次新增清单

| 名称 | 类型 | 作用 |
| --- | --- | --- |
| `testset.jsonl` | 数据 | 20 条评估样本（问题 / 参考答案 / 期望命中 chunk_id / 类型） |
| `EvalItem` / `EvalRun` | dataclass | 样本与被测系统输出 |
| `load_testset` / `run_system` | 函数 | 加载评估集、跑检索+生成 |
| `build_judge_llm` / `build_judge_embeddings` | 函数 | 本地裁判模型与向量模型 |
| `build_metrics` | 函数 | 装配六个 RAGAS 指标 |
| `_install_vertexai_shim` | 函数 | ragas 导入兼容（见 §4.4） |
| `evaluate` / `print_report` | 函数 | 检索质量汇总（Hit Rate / Recall / MRR） |
| `load_all_chunks` | 函数 | 导出全部知识块 |

---

## 4. 功能逻辑（按跳转解释）

### 4.1 评估集设计

**触发点**：`load_testset()`。

```
evaluation/testset.jsonl（每行一条）
   ├─ id                   Q01..Q20
   ├─ type                 lexical（专有名词/错误码/版本号） | semantic（语义化提问）
   ├─ question             用户提问
   ├─ reference_answer      参考答案（RAGAS 的 reference）
   └─ expected_chunk_ids   期望命中的 chunk_id（用于检索质量评估）
```

- 20 条严格按 `开发流程/06` 要求配比：**10 条专有名词类 + 10 条语义化类**。
- 期望命中项是**集合**（可多个），因为同一问题往往能从手册与历史工单两处得到答案。
- 编写前先用 `dump_chunks.py` 导出真实知识块，确保 `chunk_id` 与实际入库结果一致（不能靠记忆猜）。

**技术栈：** JSONL · dataclass

### 4.2 检索质量评估（不需要 LLM 裁判）

**触发点**：`python -m evaluation.run_retrieval_eval`

```
对每条样本：
   run_system(item, generate_answer=False)      # 只检索，不生成 → 快
      └─► hybrid_search(question, top_k)
   ├─ hit          = 期望片段 ∩ 实际返回 ≠ ∅
   ├─ recall       = |交集| / |期望|
   └─ first_rank   = 第一个期望片段的排名 → reciprocal_rank = 1/rank

汇总：
   Hit Rate@K = 命中样本数 / 总样本数
   Recall@K   = 各样本 recall 的均值
   MRR        = 各样本 reciprocal_rank 的均值
   并按 lexical / semantic 分类别统计
```

- **设计取舍**：不依赖 LLM 裁判，因此**秒级可跑、结果可复现**，适合每次改动后做快速回归。
- 未命中的样本会连同「期望 vs 实际」一起打印，直接指导下一步优化方向。

**技术栈：** 纯 Python 统计 · 复用 `hybrid_search`

### 4.3 RAGAS 六指标链路

**触发点**：`python -m evaluation.run_ragas`

```
① 对每条样本跑被测系统（含生成）→ (response, retrieved_contexts)
      └─► 组装 SingleTurnSample{user_input, response, retrieved_contexts, reference}
            └─► EvaluationDataset

② 构建裁判：ChatOllama(reasoning=False, num_ctx=8192) → LangchainLLMWrapper
   构建向量：OllamaEmbeddings                          → LangchainEmbeddingsWrapper

③ evaluate(dataset, metrics, llm, embeddings, run_config=RunConfig(timeout, max_workers))
      └─► 6 个指标并行打分，每题触发多次裁判调用

④ to_pandas() → 各指标均值 + 逐条明细 → 写入 evaluation/reports/ragas_<时间戳>.json
```

六个指标与 `开发流程/06` §6 一一对应：

| 指标 | 关注点 |
| --- | --- |
| Faithfulness | 回答是否只依据上下文（无幻觉） |
| Answer Relevancy | 回答是否切题 |
| Context Precision | 检索结果排序的精确率 |
| Context Recall | 上下文是否覆盖参考答案 |
| Answer Correctness | 与参考答案的一致度 |
| Context Entity Recall | **错误码/版本号等实体的召回**（本项目最关心） |

**技术栈：** RAGAS `evaluate` · `RunConfig`（timeout / max_workers）· LangChain Ollama 集成 · pandas

### 4.4 ragas 导入兼容（踩坑）

**触发点**：`import ragas`。

```
现象：
  ModuleNotFoundError: No module named 'langchain_community.chat_models.vertexai'
      ← ragas/llms/base.py 无条件导入 ChatVertexAI
      ← 本机 langchain_community 0.4.2 已移除该模块

方案对比：
  A. 降级 langchain_community 到 0.3.x  → 可能连带影响 langchain-core 1.6.5 / langchain-ollama 1.1.0
  B. 升级/降级 ragas                    → 0.4.3 已是最新，旧版同样有此导入
  C. 注入占位模块（采纳）                → 只影响一个用不到的类，零依赖变动

实现：在导入 ragas 之前把占位模块塞进 sys.modules
      └─► evaluation/ragas_compat.py 的 _install_vertexai_shim()
            └─► VERTEXAI_SHIM_INSTALLED 暴露是否注入过，便于日志说明
```

- 关键约束：**shim 必须先于 `import ragas` 执行**，因此 `ragas_compat.py` 内所有 ragas 导入都带 `# noqa: E402`。
- 本项目使用本地 Ollama，不涉及 VertexAI，占位类不会被实例化。

**技术栈：** `sys.modules` 注入 · `types.ModuleType`

### 4.5 裁判调用超时（踩坑）

**触发点**：首次跑 RAGAS 时。

```
现象：6 个指标任务全部 TimeoutError，所有分数为 NaN
      Evaluating: 1/6 [03:00<15:00, 180.02s/it]     ← 正好撞上 ragas 默认 180s

隔离验证（ChatOllama 单次调用耗时）：
  默认                         9.04s
  reasoning=False              0.27s   ← 快约 33 倍 ✅
  model_kwargs={think:False}   8.71s   ← langchain 不识别该参数，无效

根因：qwen3.5 是思考模型，裁判调用未关闭思考；
      6 个指标并行 → 本地模型排队 → 单次远超 180s

修正三处：
  ① ChatOllama(..., reasoning=False)        关闭思考
  ② num_ctx=8192                            默认 2048 装不下多个检索片段，会被截断
  ③ RunConfig(timeout=600, max_workers=2)   放宽超时 + 限制并发，避免本地模型被压垮
```

- 这是本项目**第三次**踩到「思考模型必须显式关闭」：重排（`think:false`）→ 生成（`think:false`）→ 裁判（`reasoning=False`）。

**技术栈：** LangChain `ChatOllama(reasoning=...)` · RAGAS `RunConfig`

---

## 5. 关键实现说明

| 项 | 说明 |
| --- | --- |
| **分层评估** | 检索质量（无裁判、秒级）与 RAGAS（有裁判、分钟级）拆成两个脚本，日常回归只跑前者 |
| **被测系统即线上链路** | `run_system` 直接调用 `hybrid_search` + `generate`，评估结果与线上一致，不是另写一套 |
| **期望命中是集合** | 一个问题的答案可能同时存在于手册与历史工单中，用集合避免误判 |
| **报告可复现** | 报告里记录 `config` 快照（模型名、top_k、rrf_k、重排开关…）与裁判模型，便于回溯 |
| **小样本冒烟** | `--limit N` 让链路验证不必等全量跑完 |
| **评估产物不入库** | `evaluation/reports/` 加入 `.gitignore`，避免仓库被时间戳文件刷屏 |

---

## 6. 与其他模块的衔接

| 衔接点 | 契约 | 状态 |
| --- | --- | --- |
| `run_system` → `hybrid_search` | `(question, top_k, rerank_enabled)` | ✅ |
| `run_system` → `generate` + `prompts` | 与线上同一套 System/User Prompt | ✅ |
| 检索优化（RRF / 重排）→ 回归门槛 | Hit Rate / Recall / MRR | ✅ 可对比 |
| LangSmith 数据集评估 | 后续可把 `testset.jsonl` 上传为数据集 | 🚧 后续 |
| CI 回归 | 后续把 `run_retrieval_eval` 接入 CI，指标下降即失败 | 🚧 后续 |

---

## 7. 验证方式

```bash
python -m evaluation.run_retrieval_eval            # 检索质量（秒级）
python -m evaluation.run_ragas --limit 3           # RAGAS 冒烟
python -m evaluation.run_ragas                     # RAGAS 全量
python -m evaluation.dump_chunks --preview 95      # 导出知识块，核对期望命中
```

**检索质量实测（Top-5，20 条）：**

```
=== 检索质量评估（Top-5，共 20 条）===
  Hit Rate@5: 100.00%
  Recall@5  : 100.00%
  MRR          : 0.8625

  分类别：
    专有名词类  n=10  hit=100.00% recall=100.00% mrr=0.9500
    语义化类   n=10  hit=100.00% recall=100.00% mrr=0.7750

  全部命中 ✅
```

> 解读：20 条全部命中目标片段；专有名词类 MRR 0.95（几乎都排第 1，说明 BM25 对错误码/版本号非常有效），
> 语义化类 MRR 0.775（偶有排到第 2~3），是后续优化的重点方向。

**RAGAS 冒烟（首次运行）：** ❌ 6 个指标全部 `TimeoutError` → NaN（即 §4.5 的问题）

**修正后：** ✅ 隔离验证 `reasoning=False` 使单次裁判调用从 9.04s 降至 0.27s；
完整六指标运行耗时较长（20 条 × 6 指标 × 多次裁判调用），执行方式见 §8。

---

## 8. 待办 / 注意

- [ ] **RAGAS 全量结果为待补基线**：命令与修正已就绪，需要一次完整运行把六个指标的基线数字落到报告里。
- [ ] **`ragas_compat` 依赖私有导入行为**：0.4.3 顶层可导入具体指标类，但若升级 ragas 需回归验证类名与 `SingleTurnSample` 字段。
- [ ] **裁判质量未校准**：本地 9B 模型作为裁判的稳定性没有验证（可与人工打分比对一小批）。
- [ ] **评估集规模偏小**：20 条够做冒烟与回归，但不足以支撑统计显著性；知识库只有 15 个片段，也需要同步扩充。
- [ ] **未做重排前后对比**：`run_retrieval_eval --rerank` 已支持，但尚未记录开启重排后的 MRR 变化。
- [ ] **`evaluation/reports/` 无清理策略**：长期运行会累积，可加保留最近 N 份。
- [ ] **CI 未接入**：目前需手动执行。

---

*（本文件为编码过程第 09 篇，记录评估层落地：检索质量基线与 RAGAS 六指标链路。）*
