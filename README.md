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

> **思考型模型必须关掉思考链**：`qwen3` / `deepseek-r1` 这类模型在 OpenAI 兼容接口下会把输出预算
> 全部消耗在思维链上，最终 `content` 为空——表现为「模型莫名其妙一直拒答」（实测 9B 模型耗时 87s 后返回空/拒答）。
> 由于只有 Ollama 原生接口支持 `think=false`，所以提供了 `LOCAL_LLM_API_STYLE=ollama`；
> 切换后同一个模型 12s 内就给出了带引用的正确答案。若你的模型不是思考型，保持 `openai` 风格即可。

向量化默认用本地 ONNX（`EMBED_BACKEND=fastembed` + `BAAI/bge-small-zh-v1.5`，512 维），无需外部服务；也可切 `litellm` 走远端 embedding 端点。

### 2.5 启动服务

```bash
python scripts/dev_services.py     # 一键前台启动 6 个在线服务（Ctrl+C 全部退出）
# 或按需单独启动
python -m uvicorn app.main:app --app-dir services/retrieval --port 8002 --reload
```

打开 <http://localhost:8000/ui/> 即为问答界面（零构建、离线可用的单页应用）：

- 左侧会话历史（localStorage 持久化，可新建/切换/删除），右侧对话区；
- **上传知识库**：侧栏「上传文档」支持点击选择或拖拽（.md/.markdown/.txt/.pdf，可多选），
  也可从服务器路径导入（文件或目录）；串行上传并在队列里逐个显示进度、分块数与 doc_id；
- **知识库管理**：侧栏「知识库」列出本文档台账（标题 / 来源路径 / 状态 / 分块数 / 大小 / 更新时间），
  支持关键字搜索、重建索引与删除（删除会同时清 Milvus、OpenSearch 与元数据）；
- 助手答案按 Markdown 渲染，`[n]` 会变成可点击角标，点击高亮并滚动到对应来源卡片；
- 来源卡片展示文档名、章节路径、原文片段与「分块号 · 字符区间 · 相关度」；
- 拒答会渲染成中性提示而非报错（拒答不是故障，是正确行为）；
- 支持浅色/深色模式、复制、重新生成、停止等待、Enter 发送 / Shift+Enter 换行；
- 输入框在等待期间禁用，避免重复提交；超时可点「停止」中断等待。

> 说明：当前为**服务端一次性返回 + 思考中提示**，不是逐 token 流式输出——
> 真流式需要 model-gateway 与 query-orchestrator 增加 SSE 透传，属于后续阶段。

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

### 2.7 评测基线（L1 确定性 + L2 RAGAS）

评测拆两层（理由见 `docs/adr/0005`）：**L1 用可判定的硬事实，不依赖裁判模型**；
L2 用 RAGAS 打答案质量分。

```bash
make corpus           # 准备语料（10 篇通用 + 3 篇权限，回读校验 ACL）
make eval-preflight   # 前置检查：语料是否入库、鉴权是否可用（秒级，不调用模型）
make eval-fast        # 只跑 L1（确定性指标，分钟级，适合改参数后反复跑）
make eval             # 全量（L1 + L2 RAGAS）
make eval-rescore     # 不重新采集，用上次落盘的采集结果重打分
```

当前基线（70 条样本、`top_k=5`、`temperature=0`、作答与裁判均为 DeepSeek、提示词 v4）：

```
L1  hit@k 100.0%（95% 区间 92.3%~100.0%）  片段召回 98.0%    引用覆盖 100.0%
    MRR(引用序) 0.9891   MRR(检索侧) 0.9891   NDCG@5 0.9864（46 条样本）
    漏答率 0.0%   误答率 0.0%   **越权泄露 0 条**   **禁用内容 0 条**   延迟 P50 688 ms / P95 1065 ms
L2  faithfulness 0.979   context_precision 0.917   context_recall 0.958   （12 条分层抽样）
```

> **片段召回 98% 是刻意保留的**：`multi-train-expense`（跨文档多跳）的两个期望分块
> 只有一个挤进 top-5——指标从贴顶变成有余量，Rerank 增益与 NDCG 才有判定空间
> （本轮 Rerank 实测正是靠它给出"零增益"的裁决，见下）。
> 延迟为多次测量中位水平；LLM 侧波动约 ±0.3s，与检索无关。

> **P50 从 1810ms 降到 610ms，不是优化了模型，是删掉了每请求一次的重复连接。**
> OPA 客户端曾每次决策都新建 `httpx.AsyncClient`：服务间 URL 写的是 `localhost`，
> 服务实际绑在 `127.0.0.1`，Windows 会先试 `::1` 再回退——每次新建连接都要付这笔延迟。
> 它极难发现：编排器自己的 `timings_ms.total` 完全正常（~0.7s），只有端到端延迟虚高。
> 排查方法：**拿各阶段计时之和与端到端对账，缺口在哪一层，问题就在哪一层。**
>
> 评测**强制绕过查询缓存**（`ChatRequest.use_cache=False`，见下文「缓存会改变可测量的东西」）。
>
> ⚠️ **L2 曾经有系统性假阴性，已修复但结论仍要记住**：逐条核对发现 12 条抽样里有
> 3 条答案是对的、裁判打了 0.25~0.5。根因有三种、全在 **statement 拆分层**：
> 把问题里的前提带进主张（问"超过三千元谁审批"→拆出"费用超过三千元"）、
> 把完整主张拆成单独看即为假的碎片（"A 与 B 共同审批"→"A 审批"+"B 审批"）、
> 引用标记 [1] 被当成内容。修法：`_to_ragas_rows` 确定性剥离 [n] +
> 给拆分器追加规则。实测 faithfulness 0.7986 → **1.0（12/12）**。
> 裁判噪声依旧存在（同一份答案连打 5 次极差 0.052），**门禁仍靠 L1 与硬不变量**
> （越权 0、禁用内容 0）。详见 `docs/adr/0005`。

改进链（每一步都可归因，逐项实测）：

| 变更 | 误答率 | 漏答率 | hit@k | L2 faithfulness | 备注 |
| --- | --- | --- | --- | --- | --- |
| 本地 4B（41 条） | 44.4% | 0.0% | 100% | 0.80 | 基线不可用 |
| 换 DeepSeek 作答+裁判 | 11.1%→20% | 0.0% | 100% | 不可归因 | 负样本扩到 20 条后暴露真实值；L2 的差异与裁判噪声同量级（见下） |
| 提示词 v2 → v3 | **0.0%** | 3.1% | 96.9% | — | 误答清零，代价是 1 条误拒 |
| 修正语料矛盾 | **0.0%** | **0.0%** | **100%** | 不可归因 | 唯一误拒源自两文档事实冲突 |

三条值得记的结论：

1. **换模型让质量数字变得可用**：代码一行未改（只改 `LLM_PROVIDER` / `JUDGE_MODEL`），
   **误答率 44.4% → 0%**、延迟减半——这正是把模型调用收敛到 model-gateway 的收益。
   ⚠️ 但**当时归给"L2 三项全面提升"的那部分现在无法确认**：L2 是裁判模型打的，
   实测同一份答案连打 5 次极差就有 0.052，与那个提升量级相当。能确定归因的只有 L1。
2. **提示词 v2 的规则写错了**：v2 说"资料只要**涉及**该话题就必须作答、不要拒答"，
   于是"主题相邻但无答案"的问题（问加州法规而资料只有本公司制度）被勉强作答。
   v3 收紧为"资料**写明答案**才作答"，误答率 20% → 0%，代价是 1 条误拒。
   顺带消除了一个隐患：拒答现在在引用映射之前短路，**拒答不会再挂着误导性引用**
   （v2 的长篇拒答因超过 80 字未被识别，曾带着 5 条不相关引用返回）。
3. **剩下那条误拒暴露的是语料缺陷，不是提示词问题**：`attendance_policy.md` 与
   `employee_handbook.md` 对"核心工作时间"给了**互相矛盾**的值，模型选择拒答而不是
   任选一个。修法不是调提示词，而是**规范值单一来源**：两份文档统一为
   `10:00 至 16:00`（原考勤制度那份自身不自洽——核心时段跨 9 小时却说可前后一小时弹性）。
   修完漏答率回到 0%。**这类冲突只能靠数据治理发现，ACL 与提示词都无能为力。**

| 类别 | 条数 | 目的 |
| --- | --- | --- |
| 单文档事实题 | 23 | 命中与排序精度（hit@k / MRR） |
| **改述题** | 5 | 同一事实换问法（口语化/同义替换），考 embedding 对措辞的鲁棒性 |
| 干扰题 | 8 | 体检费/年假/培训费/调薪/发布窗口/**病假三天/十万元/出境**在多篇文档里都出现，考察能否选对来源 |
| **跨文档多跳** | 3 | 答案横跨两篇文档（培训审批+报销时限 / 权限回收+合同留存 / 晋升条件+学时），两个分块都得进 top-k |
| 负样本（语料外） | 19 | 文档外问题必须拒答：实时/预测/隐私/代码/法规/竞品/医疗/联系/财务/闲聊/创作 + **主题相邻**（试用期工资/商业保险/带宠物） |
| **权限切片** | 8 | 同一问题不同身份期望相反：允许/拒绝各 4（HR 专属、工程专属、他人私有、跨租户） |
| **注入回归** | 2 | 间接（语料里嵌注入）/ 直接（提问里嵌注入），断言答案不出现标记串 |
| 生命周期 | 2 | 问已废止标准不引用废止文档；对照样本问现行标准 |

> **为什么从 55 扩到 70**：之前 hit@k/MRR/片段召回全部贴顶 100%，
> 指标没有区分度——Rerank 有没有增益、检索参数调没调对都无从判定。
> 扩完后片段召回 98%（`multi-train-expense` 的第二个分块没挤进 top-5），
> 指标开始"能动"了。这是把评测集当**测量仪器**用：仪器量程不够，先修仪器再谈优化。

注入回归靠 `must_not_contain` 判定：投毒内容里埋一个标记串（`INJECTION_PWNED`），
**答案一旦出现它就说明模型照做了**。这是可判定的硬事实，不依赖裁判模型，
并且和越权泄露一样是**必须为 0** 的不变量。间接注入那条还声明了期望来源——
因此模型若因文档可疑而拒答，会体现在漏答率上（那是"防护生效但行为退化"的信号，
见 `docs/adr/0007` 实测里 v3+转义那一组）。

### 回归门禁

```bash
make eval-gate        # 对比 configs/eval/baseline.json，指标不许变差
make eval-baseline    # 把当前报告冻结为新基线（显式接受现状，需 review）
```

门禁分两类判定（`scripts/check_eval_regression.py`）：

- **零容忍不变量**：`leak_count`（越权）、`forbidden_count`（注入得逞）必须为 0，
  且**字段缺失也算失败**——旧版服务产出的报告缺字段时若当成 0，门禁就被版本差异绕过了；
- **指标退化**：检索类容差 0（temperature=0 下是确定性的：`hit@k` / `MRR`（引用序）/
  `MRR`(检索侧) / `NDCG@5` / 片段召回 / 引用覆盖），拒答类容差 0.05
  （20 条负样本里 1 条就是 5%），L2 容差 **0.10**。
- **L2 的容差是实测值，不是拍的**：同一份答案、同一裁判（temperature=0）连打 5 次，
  faithfulness 落在 0.788~0.840，**极差 0.052**。原设的 0.05 比裁判噪声还小，
  门禁会随机误报。已加测试 `test_l2_tolerance_stays_above_measured_judge_noise`，
  让"调回 0.05"变成一次显式失败。

两个刻意的设计：**样本数不一致时跳过指标对比**（子集跑与全量基线的分母不同、比率不可比，
否则 CI 的限量子集会让门禁长期误报，然后被人加 `|| true` 绕过）；
**基线只有显式更新才会变**，否则每次跑批都自动"接受现状"，门禁就退化成打印当前指标。
> 反过来也成立：**基线里没有的指标，其门禁规则当前不起作用**。所以给某条新指标
> 刚加门禁规则时，门禁会显式提示"基线里没有 X…跑一次 `--update-baseline` 后才会开始比较"
> ——不提示的话，"加了规则却什么都没比"看起来和"规则通过了"一模一样。
> 这不是假设：加了 NDCG 与检索侧 MRR 的规则后，门禁输出里那两行根本没出现。

### 语料一致性检查

```bash
make corpus-check     # 规范值冲突、引用断链、重复句、占位符（秒级，读文件，不依赖模型）
```

存在理由不是假想的：实测中「核心工作时间」在两篇文档里取过不同值
（9:30-18:30 与 10:00-16:00），**用户拿到哪个答案取决于检索命中了哪一篇**。
这类缺陷 ACL 兜不住（两篇该身份都能看）、提示词也兜不住，只能靠数据治理发现——
而"人工通读几万字制度文档发现自己写了两处不同数字"不现实。

检查项与严重度（`scripts/check_corpus_consistency.py`）：

| 检查 | 严重度 | 说明 |
| --- | --- | --- |
| 规范值冲突 | error | 同一事实在不同文档取值不同。规则声明在 `configs/corpus/normative_facts.yaml`（18 条） |
| **规范值规则失效** | error | 声明了规则却一处都匹配不到（措辞改了/文档删了）。**静默变空的检查比没有检查更糟**，它会让人以为"查过了没问题" |
| 交叉引用断链 | error | 「详见《X》」而《X》不存在——引用是本文档的治理手段，断链即失去依据 |
| 占位符残留 / 文档结构 | error | TODO/待补充被带进语料；缺一级标题或短到不像一份制度 |
| 跨文档重复句 | warn | **仅报"无规则守护"的重复** |

两点值得说清的设计：

- **重复本身不是错误，规则才是让它变安全的东西**。员工手册复述工作时间、金额、时限是合理的；
  逼着所有文档都写成"详见某某制度"，会让每份文档单独都读不出东西。所以只有"既重复、
  又没有任何规则守着"才告警——那句话将来一改就是静默漂移，而没人会发现。
- **文档长度阈值刻意定得低（80 字）**，只抓"没写完就提交"。短而完整的文档（如个人笔记夹具）
  被误报会让人不再看这个检查，而它抓的不是"篇幅不够长"。

本检查已接入 `ci.yml` 的静态检查 job——它不依赖模型与知识库、秒级，
而"同一事实在两处写了不同值"恰恰是在**改文档的 PR** 里引入的。

> 它的第一次运行就抓到了一个真实冲突：`employee_handbook.md` 说病假需**二级以上医院**证明，
> `attendance_policy.md` 说连续超过三天须**三甲医院**证明——同一问题在两篇文档里答案不同。
> 修法遵循语料里已有一致的约定（专门文档负责细节、通用文档只做引用，
> 如 `expense_policy` 对体检报销、`training_policy` 对报销时限）：
> 员工手册改为「病假证明的要求详见《考勤与休假制度》」。

### 文档失效管理（已废止的文档不再被引用）

被新制度取代的旧文档，此前会**继续被检索、继续被引用**——用户拿到一份已经作废的规定，
而系统无从知道它已失效。越权有 ACL 兜着，"过期"没有人兜。

```yaml
# configs/corpus/lifecycle.yaml
documents:
  travel_allowance_2024.md:
    status: retired
    effective_to: "2025-12-31"
    superseded_by: 差旅管理制度
    note: 2024 版差旅费标准，自 2026 年起由《差旅管理制度》取代
```

链路：声明 → **入库时**判定（`packages/common/lifecycle.py`）→ 落到每个分块的
`lifecycle` 字段（Milvus 标量 + OpenSearch keyword）→ 检索时由
`compile_filters` **无条件**附加 `must_not: [{lifecycle: retired}]` → 两个 store
各自翻译（Milvus `not (...)` / OpenSearch `bool.must_not`）。

四个刻意的设计：

1. **状态只有 active / retired 两种**。区分 superseded / expired / draft 对过滤没有区别，
   细分理由放进 `superseded_by` / `note` 供人查看。状态多一个，就多一处要同步维护的语义。
2. **排除用 `must_not` 而不是 `must: {lifecycle: active}`**。后者会把**存量文档整体过滤掉**
   （没有这个字段），那是"上线即全库搜不到"的事故；前者让未声明/未迁移的文档照常可见，
   **新字段上线零迁移**。
3. **放在 `compile_filters` 而不是各调用方**。它是系统级不变量，必须不可能被忘记——
   漏传一次废止文档就会重新出现在答案里，而那不会报错。
4. **声明式而不是调接口改状态**。生命周期跟语料一样是配置，进版本库才能被 review、
   才能被一致性检查发现"这份文档的失效日期已经过了"。

台账（`GET /documents`）会返回 `lifecycle` / `lifecycle_reason` / `lifecycle_details`
（生效日期、失效日期、被谁取代）——**只存不显示等于没有**：运维无法确认废止是否生效，
也无法发现漏标。

> ⚠️ **加这个字段需要重建 Milvus 集合**：schema 没有 alter（`enable_dynamic_field=False`），
> 存量集合不会自动补列。已提供 `python scripts/rebuild_index.py`
> （先确认语料可读 → 删集合 → 从语料重新入库）。OpenSearch 侧不需要重建——
> `ensure_index` 现在会幂等 `put_mapping` 补齐缺失字段（否则 dynamic mapping 会把
> 字符串推断成 text 并分词，term 过滤匹配不上，属静默失效）。

两个必须写下来的边界：

- **日期判定发生在入库时**。文档不会在失效当天自动翻转状态，需要重新入库；
  一致性检查会对"已过失效日期"的声明告警作为兜底。
  改成"查询时注入今天"需要把日期建成可比较字段并让两个 store 都支持范围查询。
- **问已废止标准时，系统用现行标准回答而不是拒答**。实测（`lifecycle-01` 样本）：
  用户问"按 2024 版住宿标准是多少"，模型答"一线城市每晚不超过五百元 [1]"——
  废止文档内容没进上下文、引用指向现行制度，**行为是正确的**。
  但它**悄悄替换了用户问的版本**，用户可能误以为 2024 版就是五百元。
  我最初把这条样本断言成"必须拒答"，结果误答率被这条自己写的断言推到 4.8%——
  **是我的度量设计错了，不是系统错了**。现已改为断言无歧义的不变量
  （期望来源=现行制度、禁用来源=废止文档）。
  "是否该显式说明'2024 版已废止'"是一个**产品决策**，需要产品侧给出答案后再固化。

L1 指标：`hit@k`（带 Wilson 95% 区间）、`MRR`、片段召回、引用覆盖率、拒答准确率、
漏答率、误答率、**越权泄露数（必须为 0）**、**禁用内容数（必须为 0）**，
以及**检索侧排序指标** `MRR(检索侧)` / `NDCG@5` / `NDCG 样本数`，
并给出标签分组与失败样本明细。
L2 指标：`faithfulness`、`context_precision`、`context_recall`（负样本不参与）。

> 漏答率的分母是**声明了期望来源的正样本**：漏答的定义是"有答案却拒答"，
> "有答案"的证据就是期望来源。把只做内容断言的样本算进分母会让语义变模糊。

#### 两个 MRR 与一个 NDCG：数据来源不同，不能混着比

| 指标 | 数据来源 | 它实际量的是 |
| --- | --- | --- |
| `hit@k` / `MRR(引用序)` | `citations` 的顺序 | **生成侧**：模型挑了哪些引用、按什么顺序排列 |
| `MRR(检索侧)` / `NDCG@5` | 响应里的 `contexts` 顺序 | **检索侧**：RRF 融合 / 重排后的真实名次 |

两者会分开。引用顺序由生成侧决定，把它当成检索排序会得出"检索变差了"的错误结论——
这个误判我犯过一次（ADR 0005 里记着"MRR 下降不是退化"）。所以采集器现在**单独记录**
`retrieved_doc_ids`（检索侧 doc 名次）与 `chunk_doc_ids`（分块→文档归属），
且**刻意不用引用兜底**：缓存命中的响应根本没有 contexts，那种行应该被排除出检索侧分母，
而不是拿引用顺序顶替。

NDCG 用**分级相关性**（0 不相关 / 2 期望文档但切错分块 / 3 期望文档且分块里有答案原文），
指数增益 `2^rel - 1`。这比二值 NDCG 多说了一句话：检索到对的文档但切错分块，
和文档都不对，是两回事；二值指标会把它们算成同一个 0。

> ⚠️ **NDCG 与 MRR 不可直接比大小**：DCG 用对数折线 `1/log2(i+2)`，MRR 用线性折线
> `1/(i+1)`，同一个名次下 NDCG 恒大于 MRR（第 3 名 0.500 vs 0.333），只有第 1 名相等。
> 我曾把这一点写成"单期望文档时 NDCG 退化成 MRR"，是错的，已由测试钉住。
>
> `NDCG 样本数`必须与 NDCG 一起看：分母一变（新增样本、某行被判负样本）数字就不可比，
> 而光看 `NDCG@5 = 0.99` 无法知道分母是 34 还是 23。

#### 缓存会改变可测量的东西，所以评测强制绕过它

`ChatRequest.use_cache`（默认 `true`，生产行为不变）。评测固定传 `false`，两个理由都不是
"想看慢一点的数"：

1. **命中响应里没有 contexts** → 检索侧指标只能把那些行踢出分母，
   于是"缓存越多、NDCG 的样本越少"，两轮评测的 NDCG 不可比。
2. **评测若允许写缓存，会把评测流量灌进生产缓存**，让下一轮评测拿到一堆命中
   （实测：加这个开关前一轮 55 条里有 35 条是命中，延迟因此虚低到 973 ms）。

`cache_lookup` 与 `cache_store` 都读这个标志——只挡读不挡写的话，评测仍会污染缓存。

报告落盘 `eval_data/reports/`（`baseline_latest.json` 供机器比对、`baseline_latest.md` 供人读）。

> **当前基线零失败**（误答 0、漏答 0、越权 0）。但要记住两件事：
> ① 「越权」与「误答」是两类不同的问题，必须分开统计——4B 时代 alice 追问 carol 的
> 私有文档时"没拒答但引用的全是自己有权看的文档"，它该计误答、绝不能计越权；
> ② 语料外负样本已扩到 16 条，但要让误答率具备统计判定力仍需 ≥30 条
> （16 条下 0 失败的 95% 区间仍到 21%）。详见 `docs/adr/0005`。

### 2.8 权限闭环验收（S7，本项目的主线）

V1~V5 只证明「链路能跑」，且是在**单租户单部门**下跑的。要证明「权限感知」，必须用
**真实身份**跑一遍隔离矩阵：

```bash
docker compose --profile authz up -d   # Keycloak + OPA
make verify-permissions                # 清理历史产物 -> 上传权限语料 -> 跑隔离矩阵
python scripts/verify_permissions.py --skip-prepare   # 沿用现有语料，不改动知识库
```

判据（全部用 doc_id 集合断言，不依赖模型措辞）：

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
   拒答判定分三层（哨兵标记 → 固定话术 → 短句否定表述），并且**拒答一律清空 citations**——
   引用意味着「有资料支撑」，挂在拒答上等于误导用户。详见 `docs/adr/0003`。
3. **引用可定位**：`chunk` 自带 `char_start/char_end`，切分器保证 `content[char_start:char_end] == chunk.text`；`/chat` 的 `citations` 原样透传，前端可直接跳原文。
4. **混合检索用 RRF 而非分数加权**：BM25 与余弦相似度量纲不可比，RRF 只用排名，免标定。
5. **最小闭环先同步直连、Kafka 留接口**：`ChunkSink` 抽象让 ingestion→indexing 在「HTTP 直连 / Kafka 事件」之间切换时不用改业务代码。详见 `docs/adr/0001`。
6. **可选能力默认关闭且显式可观测**：改写、重排、语义缓存默认关闭；关闭时是**显式的透传**（响应里如实返回 `reranker=rrf`），而不是假装做过。
7. **幂等优先**：`doc_id` 由 `source` 稳定派生（统一 posix 分隔符），Milvus 用 `chunk_id` 作主键、OpenSearch 用 `chunk_id` 作 `_id`，重跑索引不产生重复。
8. **权限数据的失败必须大声**：`tenant/department/owner` 只能由网关从身份注入，客户端不得指定；
   `visibility=private` 缺 `owner` 直接 422，不做默认值兜底——权限字段的错误不会抛异常，
   只会"悄悄搜不到"或"悄悄越权"，是唯一必须靠端到端验收兜住的类别。详见 `docs/adr/0004`。
9. **缓存键必须包含身份维度**：查询缓存键为
   `(tenant_id, department_id, user_id, mode, top_k, temperature, query)`。
   早期实现漏了身份维度，同租户内 alice(hr) 与 bob(engineering) 问同一句问题会共用一条缓存，
   bob 直接收到带 HR 文档引用的答案——检索层 ACL 被整段绕过。
   **这类缺陷在 `CACHE_ENABLED=false` 时完全不可见**，只靠人读代码才能发现。详见 `docs/adr/0006`。
10. **评测分两层**：L1 用可判定的硬事实（越权、拒答、命中）且不依赖裁判模型，
   L2 才用 RAGAS 打答案质量分。只做 L2 会慢到没人愿意跑，且分数无法定位到样本。详见 `docs/adr/0005`。
11. **提示注入：声明 + 转义 + 检测告警，但检测不阻断**。提示词把资料声明为**数据**，
   送模型前把资料里的 `【参考资料】/【问题】/【回答要求】` 转义（文档不能伪造分节），
   可疑表达只计数告警、不改变行为——用正则决定是否拒答会把可用性押在正则的精确度上。
   受控 A/B 实测：v3 + 未转义时被投毒文档**成功劫持模型**，转义或 v4 声明层各自都能挡住。详见 `docs/adr/0007`。

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

## 7. 监控与告警

9 个服务全部暴露 `GET /metrics`（Prometheus 文本格式）。实现见
`packages/observability/metrics.py`——**没有引 prometheus_client**：需要的只是计数器/仪表/直方图
三种原语和一段曝露格式，自己写约 150 行且完全可控（取舍同"自写 RAGAS 裁判适配器"）。

| 指标 | 类型 | 标签 | 用途 |
| --- | --- | --- | --- |
| `http_requests_total` | counter | service / method / route / status | 可用性、错误率（含被拒请求） |
| `http_request_duration_seconds` | histogram | service / method / route | P95 / P99 |
| `http_requests_in_progress` | gauge | service | 拥塞判断 |
| `rag_answer_total` | counter | outcome=answered/refused/error | **拒答率与错误率** |
| `rag_cache_lookups_total` | counter | result=hit/miss/skip | 缓存命中率 |
| `rag_acl_missing_total` | counter | — | **权限下推链路断裂次数，生产必须恒为 0** |
| `rag_injection_suspected_total` | counter | source=query\|context / rule | 疑似提示注入次数（检测**不阻断**，见 ADR 0007） |

两条设计约定：

- **标签取路由模板而非实际 URL**（`/documents/{doc_id}` 而不是 `/documents/d_830daea6`）。
  否则每个文档都会生成一条时间序列，几次爬取就能把 Prometheus 打爆。匹配不到路由的一律归到
  `<unmatched>`；`/metrics` 自身不计入，避免抓取污染延迟分布。
- **指标中间件放在最外层**（starlette 越晚添加越靠外层）。401/429 这类被拒请求必须计入可用性与
  错误率——漏掉它们会让错误率偏低，而"错误率偏低"正是监控造假最常见的形式。抛异常的请求同样
  计为 500。

`METRICS_TOKEN` 的行为要分清两层（实测确认，不是推断）：

- **token 是全局的**：一旦配置，**所有**服务的 `/metrics` 都要求 `Authorization: Bearer <token>`。
  指标会暴露内部路由与流量形态，不该裸奔；配了就统一要求，比"只有网关要"更容易推理。
  ⚠️ 这意味着 prometheus.yml 的**两个 job 都要带凭据**——只给网关带，
  内部服务会在配好 token 的那一刻集体 403。
- **网关额外有一层**：它的 `/metrics` 只在配了 token 时才免**用户鉴权**
  （Prometheus 没有 Keycloak 令牌）；不配 token 则先被身份中间件拦成 401，这是刻意的安全默认。
- 不配 token（本地开发）：所有服务都直接可达。

抓取配置与告警规则在 `infra/monitoring/`：

```bash
# 告警规则的组织原则：把验收清单里的不变量直接写成表达式，而不是先看有哪些指标可告
infra/monitoring/alerts.yml      # RagAclMissing / RagPromptInjectionInContext / RagServiceDown /
                                 # RagHighErrorRate / RagHighP95Latency / RagRefusalRateHigh /
                                 # RagCacheHitRateLow
infra/monitoring/prometheus.yml  # 内部服务一组 + 网关一组（带 token）
```

其中 `RagAclMissing`（`increase(rag_acl_missing_total[5m]) > 0`，`for: 0m`）对应验收里的
**绝对不变量**：越权/权限下推断裂出现一次就立刻告警，不等趋势。

> 仓库**不**附带 docker-compose 的 Prometheus 服务：本地开发时应用服务由 `scripts/dev_services.py`
> 启动并绑定 `127.0.0.1`，容器内既看不到宿主 loopback、也用不通 `host.docker.internal`。
> 与其塞一个跑不通的块，不如把配置写清楚，由部署形态决定怎么跑（k8s 换成服务 DNS 或
> 用 helm 预留的 `podAnnotations` 钩子走注解发现）。

## 8. 当前边界（明确未完成）

| 项 | 现状 | 后续 |
| --- | --- | --- |
| Keycloak / OPA | 代码与策略就绪，默认 `AUTHZ_ENABLED=false` 走固定身份 | 打开开关即启用，无需改代码 |
| Kafka | 仅占位（拓扑与接口已定），默认同步直连 | `USE_KAFKA=true` 切异步 |
| Langfuse | 未配置密钥时静默降级为 no-op | 配置 `LANGFUSE_*` 即开始上报 |
| 鉴权与授权 | 已接入 Keycloak（JWT + JWKS，验签失败自动刷新）与 OPA（默认拒绝白名单），端到端验收通过 | 字段级/文档级授权、令牌静默刷新、生产用 HTTPS + PKCE 回调域名 |
| 角色白名单可见性 | `allowed_roles` 已入契约但未参与过滤 | 补存储层 schema/expr 与两个入口的字段传递 |
| 语义缓存 | 当前为精确匹配 | 已有 eval set，可升级为 embedding 相似度匹配（须同时保证身份隔离） |
| Reranker | 已实现并接线（中文可用的 bge-reranker-base）；**默认关闭，两轮实测维持关闭** | 第一轮（指标贴顶）无法判定；第二轮在有余量的 70 条难集上真实开启：质量四项**分毫不差**（multi-hop 缺口也没救回），P50/P95 852/1070 → 10782/22214ms（**12.7x/20.8x**，CPU）。翻案条件：更强 reranker/GPU，或评测集出现"RRF 排错、重排能纠"的具体案例 |
| 标准 Recall@K | 仍以 `hit@k` 代替（"至少命中一条期望来源"，比分母意义上的 Recall 宽松） | 声明多个期望来源并在评测集里出现"部分命中"样本后，改为按期望来源计的召回率 |
| 中文分词 | OpenSearch 用 `standard` 分析器 | 换带 IK 插件的镜像并重建索引 |
| PDF / Word / HTML | 已支持 Markdown / Txt / PDF（pypdf，无 OCR） | 补 docx / html / OCR |
| 前端 | `api-gateway` 内置单页应用（问答 + 上传 + 知识库台账 + 深色模式） | `apps/chat-ui`、`apps/admin-console`（Next.js，含评测看板、批量导入、权限配置） |
| 文档管理 | 列表 / 关键字搜索 / 重建索引 / 删除（`GET/DELETE /documents`） | 批量上传任务化、版本历史、失败重试 |
| 批量上传 | 前端串行逐个上传（避免打满写入路径） | 改走 Kafka 异步通道（`USE_KAFKA=true` 时 ingestion 已支持） |
| 流式输出 | 一次性返回 + 思考中提示 | model-gateway → orchestrator → gateway 三级 SSE 透传（LangGraph `astream` 已可提供节点级进度） |
| 拒答的兜底判定 | 哨兵 + 固定话术 + 短句启发式（阈值 80 字） | 用评测集标定「相关性阈值」，让不可回答的问题在检索阶段就返回空 |
| 入库吞吐 | 单文档 `/index` 因 Milvus `flush` + OpenSearch `refresh` 约 20s（本机实测） | 大文档改批量写入 + 关闭同步 refresh，用 bulk 参数控制可见性 |
| 告警通道 | ✅ 已接线：8 条规则 + `alertmanager.yml`（critical/warning 分路）+ compose `monitoring` profile（prometheus+alertmanager 一键起，`scripts/dev_monitoring.ps1`）。webhook 接收器是占位符，接真实 IM/工单时替换 URL | 把占位 webhook 换成钉钉/企微/Slack；Grafana 看板 |
| 监控看板 | 无 Grafana 看板 | 按第 7 节的指标表建四块面板：P95 / 错误率 / 拒答率 / 缓存命中率 |
| SLO 阈值 | ✅ 已成文（`docs/slo.md`）：每条 SLO 都注明度量手段与对应告警；明确列出**不设 SLO 的项**及理由 | 按月复核 SLO 与实测的差距，调整告警阈值 |
| 服务间身份信任 | 编排与检索从**明文 header** 取身份（网关是唯一鉴权点）——已 fail-closed：缺头即 403，不再静默用默认租户 | mTLS 或服务网格；当前不可达（应用服务不发布端口 + NetworkPolicy），属纵深防御加固 |
| 提示注入 | 三层已实现（声明 + 结构转义 + 检测告警，见 `docs/adr/0007`），并有 2 条常驻回归样本 + 零容忍门禁。残余风险：检测只覆盖已知表达形式；转义只覆盖当前模板用的那三个标记 | 模板改动时同步 `_FORGEABLE_SECTIONS`；`data/corpus/injection_probe.md` 是**故意投毒**的夹具，勿当垃圾清理 |
| 语料一致性 | 已有机制：18 条规范值规则 + 引用断链 + 无规则守护的重复句，接入 CI（`make corpus-check`）。已抓出并修掉两例真实冲突（核心工作时间、病假证明） | 规则覆盖范围仍是人工挑选的；**没有规则守着的事实仍可能互相矛盾**，补规则是持续动作 |
| 上传内容校验 | 只做大小与非空校验，**无 MIME / 内容类型校验、无投毒检测** | 加类型白名单与内容扫描；投毒目前靠 `RagPromptInjectionInContext` 告警兜住 |
| 数据失效管理 | ✅ 已实现：声明式生命周期（`configs/corpus/lifecycle.yaml`）+ 库层过滤（`must_not`），已废止文档不参与检索且台账可见。残余：日期判定在**入库时**，跨失效日不会自动翻转，需重新入库（一致性检查会提醒） | 自动翻转可改为把生效/失效日期建成可比较字段并在查询时注入"今天"；多版本并存与"指向新版"的答案提示 |
| 成本核算 | ✅ 已折算：`rag_llm_cost_usd_total{model,tenant}` 按模型单价计美元（`LLM_PRICES` 可配，默认 DeepSeek 官方价），按租户+日累计进 Redis，`GET /admin/quotas/{tenant}` 返回 `cost_today_usd`。单价会漂移，**以供应商账单为准校准** | 对接账单系统；按成本维度做配额 |
| 审计日志 | ✅ 已落库：网关访问审计进 Postgres `audit_logs`（谁/何时/访问什么/结果），`GET /admin/audit` 可按租户与用户检索（需 rag_admin）。队列+批量写，失败丢弃并告警日志，不影响业务请求 | 审计行加保留期与自动归档；记录"访问了哪些文档"需在编排层补 |

> 实测记录（本机 Docker + CPU 推理）：`/chat` 端到端约 2–14s，其中检索 ~0.3s、生成 2.3–12.6s；
> `/index` 单文档（10 分块）约 20s，瓶颈在 Milvus flush 与 OpenSearch refresh，不在向量化。

历史 P0 设计与坑位清单见 `docs/architecture/minimal-loop-v0.md`。
