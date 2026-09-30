# 07-LangGraph 编排闭环

> 加入日期：2026-09-30
> 关联设计：`开发流程/04-检索与编排设计.md` §3、`开发流程/03-MVP 最小闭环.md`
> 新增文件：`app/graph/build.py`、`app/graph/nodes/*`（6 个）、`app/generation/ollama.py`、`tests/test_graph.py`
> 改动文件：`app/graph/state.py`、`app/generation/prompts.py`、`app/memory/checkpointer.py`、`app/core/config.py`、`.env.example`、`开发流程/04`（勘误）

---

## 1. 在整体布局中的位置（从底层到总体）

本次把检索与生成能力**串成业务闭环**，是项目从「能查」到「能办事」的转折点。

```
第 6 层  部署            （待实现）
第 5 层  接入            app/api/*            ← 下一步：调 get_graph()
第 4 层  编排            app/graph/           ← 本次完成
                         ├── build.py         图装配 + 条件路由
                         ├── state.py         TicketState（唯一数据载体）
                         └── nodes/           intent / retrieve / draft / review / finalize / send
                              │
第 3 层  能力            app/retrieval/*（检索）· app/generation/*（生成）· app/memory/*（状态）
                         └── generation/ollama.py ← 本次补上（draft 节点依赖）
                         └── memory/checkpointer.py ← 本次完成（SQLite）
第 2 层  契约与配置       app/core/config.py      ← 新增敏感词、审核开关、生成超时
第 1 层  运行时           Milvus + Ollama
```

- **依赖谁**：检索层（`hybrid_search`）、生成层（`generate`）、持久层（Checkpointer）、配置。
- **被谁依赖**：接入层（`api/ticket.py`）将调用 `get_graph()`；`api/session.py` 读 Checkpointer 快照。
- **为什么要有它**：前面所有能力都是「无状态的函数」。有了它才有**状态、分支、人工介入与断点恢复**。

**技术栈：** LangGraph（`StateGraph` / 条件边 / `interrupt` / `Command`）· langgraph-checkpoint-sqlite · Ollama（`/api/chat`）· Python TypedDict

---

## 2. 加入后的项目整体布局

```
app/
├── graph/
│   ├── state.py           # TicketState（可 JSON 序列化）
│   ├── build.py           # ← 图装配 + 3 个路由函数 + get_graph()
│   └── nodes/
│       ├── intent.py      # ← 敏感关键词 → need_review
│       ├── retrieve.py    # ← 调 hybrid_search，转 dict 存状态
│       ├── draft.py       # ← 调 generate，失败降级为片段摘录
│       ├── review.py      # ← interrupt 挂起 / resume 恢复
│       ├── finalize.py    # ← 合并 draft / edited_reply
│       └── send.py        # ← 幂等发送 + 状态落库
├── generation/
│   ├── ollama.py          # ← 本次实现：generate / generate_stream
│   └── prompts.py         # ← 本次增强：build_fallback / NOT_FOUND_REPLY
└── memory/
    └── checkpointer.py    # ← 本次实现：SQLite / memory 双后端
```

---

## 3. 本次新增清单

| 名称 | 类型 | 作用 |
| --- | --- | --- |
| `build_graph` / `get_graph` | 函数 | 装配并编译图（单例） |
| `route_after_retrieve` | 函数 | 有命中 → draft；无命中 → finalize |
| `route_after_draft` | 函数 | 需审核 → review；否则 → finalize |
| `route_after_review` | 函数 | approved → finalize；rejected → END |
| `intent_node` / `detect_intent` | 函数 | 意图识别与敏感词匹配 |
| `retrieve_node` | 函数 | 混合检索 + 结果可序列化 |
| `draft_node` | 函数 | 生成草稿 + 失败降级 |
| `review_node` | 函数 | `interrupt` 挂起 + 载荷校验 |
| `finalize_node` | 函数 | 三级优先级定稿 |
| `send_node` | 函数 | 幂等发送 |
| `generate` / `generate_stream` | 函数 | Ollama 调用（含重试） |
| `get_checkpointer` / `thread_config` | 函数 | 状态持久化 |
| `sensitive_keywords` / `require_review_for_all` / `llm_timeout_seconds` | 配置 | 审核策略与生成超时 |

---

## 4. 功能逻辑（按跳转解释）

### 4.1 图拓扑与路由决策

**触发点**：`graph.invoke({session_id, ticket_id, query}, config)`

```
START
 └─► intent         敏感关键词匹配 → intent / need_review
      │
      ▼  （无条件边：草稿必须有依据，所以无论是否敏感都要检索）
   retrieve          hybrid_search → retrieved(dict) + retrieval_debug
      │
      ├─(retrieved 非空)──► draft       组装 Prompt → Qwen 生成
      │                       │
      │                       ├─(need_review=True)──► review
      │                       └─(need_review=False)─► finalize
      │
      └─(retrieved 为空)───────────────────────────► finalize  →「未找到」回复

   review ─┬─(approved)─► finalize
           └─(rejected)─► END        驳回不发送

   finalize ─► send ─► END
```

- **勘误**：`开发流程/04` §3.3 初稿把「敏感工单直接跳 draft（跳过检索）」画成了一条边，属于设计失误。本次实现按「先检索、后决定是否审核」修正，并已回写文档。

**技术栈：** LangGraph `StateGraph.add_conditional_edges`（路由函数返回字符串键）

### 4.2 人工审核的挂起与恢复

**触发点**：敏感工单进入 `review` 节点。

```
① 首次调用
   review_node(state)
     └─► interrupt({ticket_id, draft, citations, reason})
           └─► 图在此挂起，invoke 返回，结果含 __interrupt__
                 └─► 状态已由 Checkpointer 落盘（此时 send_status 为空，绝不会误发）

② 人工在客服台审核后
   graph.invoke(Command(resume={"decision":"approved", "edited_reply":"..."}), config)
     └─► interrupt() 返回该载荷 → 继续执行
           └─► 路由到 finalize → send
```

- **载荷校验**：`resume` 值不是 dict、或 `decision` 不在白名单内 → **一律按驳回处理**（宁可多发一次审核，不可误发）。
- `edited_reply` 存在时优先于 `draft` 作为最终文案。

**技术栈：** `langgraph.types.interrupt` / `Command` · 同一 `thread_id` 恢复

### 4.3 Checkpointer 与 thread_id

**触发点**：图编译时绑定；每次 invoke 用 `config` 定位会话。

```
get_graph()（lru_cache 单例）
   └─► build_graph(checkpointer=get_checkpointer())
         └─► get_checkpointer()
               ├─ backend == "memory" ─► InMemorySaver（重启即丢，仅演示）
               └─ 默认 sqlite        ─► sqlite3.connect(path) → SqliteSaver.setup()
                                         └─► 首次自动建表，幂等
thread_config(session_id) → {"configurable": {"thread_id": session_id}}
```

- `check_same_thread=False`：FastAPI 的线程池会跨线程复用连接。
- 会话键直接用 `session_id`，与接口契约（`/session/{id}`）对齐。

**技术栈：** `langgraph.checkpoint.sqlite.SqliteSaver` · SQLite3

### 4.4 生成节点与降级

**触发点**：`draft_node` 调用 `generate(SYSTEM_PROMPT, USER_PROMPT_TEMPLATE)`。

```
generate() → POST /api/chat  (stream=false, think=false, temperature=0.2)
   ├─ 成功 ─────────────► draft = 模型输出；citations = 检索片段 id
   └─ 抛 AppError（生成服务不可用，已重试 2 次）
         └─► 降级：build_fallback(query, chunks) → 直接摘录 Top-3 片段原文
               └─► state.error 记录 "draft_fallback: ..."
```

- 降级的意义：**知识片段仍然给到人工**，不至于整单失败。
- 同样必须 `think: false`（与重排同源的经验）。

**技术栈：** httpx（流式/非流式）· 指数退避重试 · Ollama `/api/chat`

### 4.5 发送幂等

**触发点**：`send_node`。

```
state.send_status == "sent"? ──是──► 直接返回 sent（跳过重复发送）
                              └─否─► reply 为空? ──是──► failed
                                                    └─否─► 发送 + sent
```

- 真实发送适配器（工单系统 API / 邮件 / IM）留待后续；当前只维护状态机。

**技术栈：** Python 状态判断

### 4.6 状态可序列化（本次踩的坑）

**触发点**：敏感工单恢复执行时，Checkpointer 反序列化状态。

```
初版：retrieved = [chunk.model_dump() ...]
        └─► Python 模式下 Enum 保持为实例 → 状态里混入 DocSource 枚举
              └─► 恢复时报：
                  "Deserializing unregistered type app.schemas.retrieval.DocSource
                   from checkpoint. This will be blocked in a future version."

修正：retrieved = [chunk.model_dump(mode="json") ...]
        └─► 枚举转成字符串值 → 状态纯 JSON → 告警消失
```

- 同时把 `history` 的类型从 `list[Message]` 改为 `list[dict]`，要求接入层传 dict。
- 顺带收益：整个 State 都可 `json.dumps`，直接可用于 SSE 回传与观测上报。

**技术栈：** Pydantic `model_dump(mode="json")`

---

## 5. 关键实现说明

| 项 | 说明 |
| --- | --- |
| **无条件检索** | `intent → retrieve` 是普通边；敏感与否只影响 `draft` 之后的分支。避免「敏感工单草稿没有依据」 |
| **审核载荷防御** | 非 dict、未知 decision 一律按 `rejected` 处理，保证「绝不误发」 |
| **驳回不发送** | `review → END` 直接结束，不经过 `finalize`/`send`，状态里没有 `reply`/`send_status` |
| **无命中也有回复** | 无命中走 `finalize` → `NOT_FOUND_REPLY` → 仍会 `send`（相当于自动回执并建议转人工） |
| **状态纯 JSON** | 见 4.6，这是让 Checkpointer 长期稳定工作的前提 |
| **敏感词可配置** | 默认 11 个词（投诉/举报/起诉/法律/赔偿/泄露/数据丢失/安全事件/监管/停机事故/账号被盗），走环境变量 |
| **`REQUIRE_REVIEW_FOR_ALL`** | 一键把「普通工单」也纳入人工审核，便于灰度期先全量审核 |

---

## 6. 与其他模块的衔接

| 衔接点 | 契约 | 状态 |
| --- | --- | --- |
| `api/ticket.py` → `get_graph().invoke` | `TicketQueryRequest` → 初始 state | 🚧 下一步 |
| `api/ticket.py` → `Command(resume=...)` | `ReviewRequest` → 审核载荷 | 🚧 下一步 |
| `api/session.py` → `graph.get_state(config)` | `thread_config(session_id)` → 快照 | 🚧 下一步 |
| `retrieval_debug` → LangSmith | 状态里已备好，待接观测 | 🚧 后续 |
| `send_node` → 真实工单系统 | 待接入发送适配器 | 🚧 后续 |

---

## 7. 验证方式

```bash
pytest -q                       # 84 passed
python -m ingestion.ingest      # 前置：知识库需已入库
```

**单元测试（22 个，检索与生成全部 mock）：**

| 类别 | 覆盖 |
| --- | --- |
| 意图识别 | 命中敏感词 / 默认 consult |
| 路由函数 | 3 个路由函数的全部分支（含缺字段的兜底） |
| 定稿优先级 | edited_reply > draft > 未找到；驳回防御 |
| 发送 | 正常 / 已发送幂等 / 空回复 |
| 图：普通工单 | 走完全链路 → `send_status=sent` |
| 图：无命中 | 回复含「未找到」并发送 |
| 图：敏感工单 | 挂起且 `send_status` 为空 |
| 图：审核通过 | `approved` → 发送；`edited_reply` 生效 |
| 图：审核驳回 | 无 `reply` / 无 `send_status` |
| 图：载荷异常 | 按驳回处理 |
| 图：生成失败 | 降级为片段摘录且仍发送 |
| 状态序列化 | `json.dumps(retrieved)` 通过，`source` 为字符串 |

**真实环境闭环实测（Milvus 15 片段 + Ollama `qwen3.5:9b`）：**

```
=== 场景 1：普通工单（自动成稿并发送）===
  耗时=19.0s
  intent=consult need_review=False review=None send=sent
  citations=['T-1001#0', 'faq-common-questions#4', 'manual-login-module#4', ...]
  检索: mode=hybrid dense=15 sparse=15
  回复（节选）:
    处理步骤如下：
    1. 通知管理员确认 migrate_auth.py 已执行 [来源: T-1001#0]
    2. 所有用户需要重新登录一次，这是令牌签名算法变更导致的预期行为 [来源: T-1001#0]
    3. 管理员需核对 config 目录是否已备份 [来源: faq-common-questions#4]

=== 场景 2：敏感工单（挂起 → 审核 → 恢复发送）===
  首次调用耗时=22.4s
  是否挂起: True
  挂起原因: 命中敏感关键词
  挂起时是否已发送: None   ← 关键：挂起态绝不发送

  --- 人工审核通过 ---
  intent=sensitive need_review=True review=approved send=sent
  回复: （人工确认）请通知管理员确认 migrate_auth.py 已执行，用户重新登录即可。

=== 场景 3：Checkpointer 持久化校验 ===
  恢复后可从 Checkpointer 读到状态: True
  ticket_id=T-A2 send_status=sent
```

> 首次运行时还观察到 Checkpointer 的 `unregistered type` 告警，即 4.6 描述的序列化问题；修正后重跑告警消失。

---

## 8. 待办 / 注意

- [ ] **单工单耗时约 20s**：本地 `qwen3.5:9b` 生成是主要瓶颈。接 SSE 流式后体感会改善，但仍需评估是否换 4b 模型。
- [ ] **`generate_stream` 尚未被使用**：`draft_node` 目前调用非流式 `generate`；SSE 节点需要改为流式并逐 token 推送（下一步接入层会用到）。
- [ ] **`send_node` 未接真实通道**：仅维护状态，幂等也只覆盖「同一状态重复进入」。
- [ ] **多轮追问未实现**：`history` 字段已预留但节点未使用；需要增加查询改写节点（v1.1）。
- [ ] **审核超时未处理**：挂起后若无人审核会永久停留；需要 SLA 提醒与超时策略（v1.2）。
- [ ] **`get_graph()` 是进程级单例**：SQLite 连接也随之单例，多进程部署（uvicorn workers>1）需改用 Postgres/Redis。
- [ ] **状态清理**：`SESSION_TTL_DAYS` 配置已存在但尚无清理任务，SQLite 会持续增长。
- [ ] 敏感词命中是**纯包含匹配**，容易被误触发（如「投诉渠道在哪里」）；后续需升级为规则/模型判定。

---

*（本文件为编码过程第 07 篇，记录 LangGraph 编排闭环的落地与实测。）*
