# 08-接入层与 SSE 流式

> 加入日期：2026-09-30
> 关联设计：`开发流程/05-接口与数据契约设计.md` §4~§5
> 新增文件：`app/api/sse.py`、`tests/test_api.py`
> 改动文件：`app/api/ticket.py`、`app/api/session.py`、`app/api/retrieval.py`、`app/graph/nodes/draft.py`、`app/memory/checkpointer.py`、`app/graph/build.py`、`app/main.py`

---

## 1. 在整体布局中的位置（从底层到总体）

本次把编排闭环**暴露成真正的服务**，项目从「能跑通」变成「能对外用」。

```
第 6 层  部署            deploy/（待补 compose 的 api 服务联调）
第 5 层  接入            app/api/            ← 本次完成（4 个接口全部可用）
                         ├── sse.py          事件编码与响应头
                         ├── ticket.py       /ticket/query(SSE) · /ticket/review
                         ├── session.py      /session/{id}
                         ├── retrieval.py    /retrieval/search
                         └── health.py       /health（已完成）
第 4 层  编排            app/graph/          ← 本次改造：异步图工厂 get_graph()
第 3 层  能力            retrieval / generation / memory
                         └── memory/checkpointer.py ← 本次改造：AsyncSqliteSaver
第 2 层  契约与配置       schemas（沿用既有契约）
第 1 层  运行时           Milvus + Ollama
```

- **依赖谁**：编排层（`get_graph()`）、检索层（`hybrid_search`）、契约层。
- **被谁依赖**：客户端 / 工单系统（HTTP + SSE）。
- **为什么要有它**：闭环本身只是「函数」。接入层负责**协议转换**（HTTP ↔ 图状态）、**流式体验**（逐 token）与**错误契约**（业务错误码）。

**技术栈：** FastAPI · Starlette `StreamingResponse`（SSE）· LangGraph `astream(stream_mode=["updates","custom"])` · `get_stream_writer` · asyncio · httpx（验证）

---

## 2. 加入后的项目整体布局

```
app/
├── api/
│   ├── sse.py          # ← 新增：SSE 编码 + 响应头
│   ├── ticket.py       # ← 完成：SSE 流式 + 审核恢复
│   ├── session.py      # ← 完成：会话快照查询
│   ├── retrieval.py    # ← 完成：检索调试
│   ├── health.py       #    依赖探测
│   └── router.py       #    路由汇总
├── graph/
│   ├── build.py        # ← 改造：get_graph() 改异步 + reset_graph()
│   └── nodes/draft.py  # ← 改造：显式开关控制是否逐 token 流式
├── memory/
│   └── checkpointer.py # ← 改造：AsyncSqliteSaver + 生命周期管理
└── main.py             # ← 改造：lifespan 预热图 / 关闭连接
tests/
└── test_api.py         # ← 新增 15 个用例
```

---

## 3. 本次新增清单

| 名称 | 类型 | 作用 |
| --- | --- | --- |
| `format_sse` / `SSEWriter` | 类/函数 | SSE 文本编码（含自增 id） |
| `SSE_HEADERS` | 常量 | 禁用缓冲的响应头 |
| `_event_stream` | 函数 | 把图流翻译成 SSE 事件 |
| `query_ticket` | 接口 | 提交工单（SSE） |
| `review_ticket` | 接口 | 提交审核并恢复图 |
| `get_session` | 接口 | 读 Checkpointer 快照 |
| `search` | 接口 | 检索调试 |
| `streaming_thread_config` | 函数 | SSE 专用 config（开启 token 流） |
| `get_graph`（异步）/ `reset_graph` | 函数 | 异步图单例 |
| `get_checkpointer` / `close_checkpointer` | 函数 | 异步 Checkpointer 生命周期 |
| `_stream_enabled` / `_stream_draft` | 函数 | 草稿节点的流式分支 |

---

## 4. 功能逻辑（按跳转解释）

### 4.1 SSE 事件流

**触发点**：`POST /api/v1/ticket/query`

```
请求 → 校验参数 → 守卫检查（该会话是否已有工单卡在审核） → 返回 StreamingResponse

_event_stream(state, config)
  ├─► yield "start"
  ├─► async for (mode, chunk) in graph.astream(..., stream_mode=["updates","custom"])
  │      ├─ mode == "custom" ──► 草稿节点推来的 {"event":"token","delta":...}
  │      │                        └─► yield "token"
  │      ├─ chunk 含 __interrupt__ ──► yield "review_required" → return（流结束）
  │      └─ mode == "updates"
  │             ├─ intent   ──► yield "intent"   {intent, need_review}
  │             ├─ retrieve ──► yield "retrieval"{mode, hit_count, top_docs}
  │             └─ draft    ──► yield "draft"    {draft, citations}
  ├─► await graph.aget_state(config) → yield "done" {reply, send_status, citations}
  └─ 任意异常 ──► yield "error" {code, message}
```

- **两种流模式组合使用**：`updates` 提供节点级进度，`custom` 提供 token 级增量。
- 事件契约（`开发流程/05` §5）逐字对齐：`start / intent / retrieval / token / draft / review_required / done / error`。

**技术栈：** Starlette `StreamingResponse` · LangGraph `astream` 多模式 · async generator

### 4.2 逐 token 流式的开关（踩坑）

**触发点**：`draft_node` 决定走流式还是非流式。

```
初版思路：get_stream_writer() 取不到就非流式
   └─► 问题：**非流式 invoke 下它也返回一个可调用的 writer**（只是没人消费）
         └─► 于是非流式调用也走逐 token 分支
               └─► 症状：单元测试静默调用真实模型（测试从 2.6s 涨到 45s）

修正：由调用方在 config 里显式声明
   api/ticket.py → streaming_thread_config(session_id)
        └─► config.configurable.stream_tokens = True
              └─► draft_node 读 config 决定是否用 generate_stream
```

- 收益：行为可预测、可测试；非流式路径（脚本/测试）不会被流式分支吞掉。

**技术栈：** LangGraph 节点注入 `RunnableConfig` · `get_stream_writer()`

### 4.3 审核恢复

**触发点**：`POST /api/v1/ticket/review`

```
读 Checkpointer 快照
   ├─ snapshot.next 为空 ──► 409（该会话没有待审工单）
   ├─ 快照里的 ticket_id ≠ 请求的 ──► 409（防止审错单）
   └─ 通过 ──► graph.ainvoke(Command(resume=payload), config)
                 └─► 图从 review 节点继续 → finalize → send
                       └─► 返回 {review_status, send_status, reply}
```

- `exclude_none=True`：只把有值的字段传回，避免 `edited_reply=None` 覆盖人工已改内容。

**技术栈：** `langgraph.types.Command(resume=...)` · Pydantic `model_dump(exclude_none=True)`

### 4.4 会话查询

**触发点**：`GET /api/v1/session/{session_id}`

```
aget_state(thread_config(session_id))
   ├─ values 为空 ──► 404（会话不存在/已过期）
   └─ 有值 ──► 由状态推导 ticket.status
                 ├─ snapshot.next 非空   → awaiting_review
                 ├─ review_status=rejected → rejected
                 ├─ send_status=sent       → sent
                 ├─ send_status=failed     → failed
                 └─ 其他                    → processing
```

**技术栈：** LangGraph `aget_state` · 状态 → 视图映射

### 4.5 检索调试接口

**触发点**：`POST /api/v1/retrieval/search`

```
asyncio.to_thread(hybrid_search, ...)   ← 同步且慢（含 embedding），放线程池
   └─► 映射为 SearchResponse{mode, dense_hits, sparse_hits, fused, reranked, chunks}
```

**技术栈：** `asyncio.to_thread`（避免阻塞事件循环）

### 4.6 Checkpointer 必须换异步实现（踩坑）

**触发点**：首个接口请求。

```
现象：所有接口 500
日志：NotImplementedError: The SqliteSaver does not support async methods.
      Consider using AsyncSqliteSaver instead.

根因：接入层用 astream / ainvoke / aget_state，而同步 SqliteSaver 不支持任何 async 方法

修正：
  checkpointer.py → AsyncSqliteSaver(aiosqlite.connect(path)) + await setup()
  build.py        → get_graph() 改为 async（首次调用时异步初始化）
  main.py         → lifespan 里预热 get_graph()，退出时 close_checkpointer()
```

- 连带影响：脚本与测试若用同步 `graph.invoke` 会失败，需改用 `ainvoke`。

**技术栈：** `langgraph.checkpoint.sqlite.aio.AsyncSqliteSaver` · aiosqlite · FastAPI lifespan

---

## 5. 关键实现说明

| 项 | 说明 |
| --- | --- |
| **SSE 响应头** | `Cache-Control: no-cache` + `X-Accel-Buffering: no`，避免 nginx 等反代缓冲导致「假流式」 |
| **事件 id 自增** | 由 `SSEWriter` 统一维护，为后续断线重连（`Last-Event-ID`）留出基础 |
| **先守卫后流式** | 会话卡在审核时**在返回 StreamingResponse 之前**就抛 409（普通 JSON），客户端能直接读到错误码 |
| **错误也走 SSE** | 流已开始后的异常无法再改 HTTP 状态，统一用 `error` 事件收尾，不静默中断 |
| **图预热** | lifespan 里 `await get_graph()`，首个请求不必承担 Checkpointer 初始化与图编译开销 |
| **审核双重校验** | 校验「有待审工单」+「ticket_id 一致」，防止误审批到别的工单 |
| **状态快照单一来源** | 会话接口直接读 Checkpointer，不另建数据库表，避免状态双写不一致 |

---

## 6. 与其他模块的衔接

| 衔接点 | 契约 | 状态 |
| --- | --- | --- |
| 客户端 → `/ticket/query` | SSE 事件见 §4.1 | ✅ 已验证 |
| 客户端 → `/ticket/review` | `ReviewRequest` → `Command(resume=...)` | ✅ 已验证 |
| `/retrieval/search` → `hybrid_search` | 直接透传参数与结果 | ✅ 已验证 |
| `send_node` → 真实工单系统 | 待接入发送适配器 | 🚧 后续 |
| LangSmith 埋点 | 事件与状态已就绪，待接 SDK | 🚧 后续 |
| Docker Compose 联调 | `api` 服务已在 compose 中，待端到端联调 | 🚧 后续 |

---

## 7. 验证方式

```bash
pytest -q                                              # 98 passed
uvicorn app.main:app --port 8000                       # 启动服务
```

**单元测试（15 个，检索/生成全 mock，Checkpointer 用内存）：**

| 类别 | 覆盖 |
| --- | --- |
| SSE 普通工单 | 200 + `text/event-stream` + `X-Trace-Id`；事件序列含 start/intent/retrieval/token/draft/done；token 可拼回草稿 |
| SSE 敏感工单 | 以 `review_required` 收尾，**不含 done** |
| 并发守卫 | 挂起期间再提交 → 409/1005 |
| 审核 | approved 发送；rejected 不发送；无待审 → 409；ticket_id 不符 → 409 |
| 会话查询 | 正常返回 + status 推导；awaiting_review 标记；未知会话 → 404 |
| 检索调试 | 结果正确映射 |
| 参数校验 | 空 query → 400/1001；非法 decision → 400/1001 |

**真实服务实测（uvicorn + Milvus + Ollama `qwen3.5:9b`）：**

```
=== 1) 普通工单 SSE ===
  status=200  content-type=text/event-stream; charset=utf-8
  X-Trace-Id=c43fe3561b794001
  retrieval: mode=hybrid hits=5
  事件序列: [start, intent, retrieval, token, token] ... [token, draft, done]  共 188 个事件
  首 token 延迟: 3.9s
  总耗时: 19.8s
  done.reply: 升级至 v1.3.0 后所有同事被强制退出登录属于**预期行为**…
             1. 通知管理员确认脚本执行 [来源: T-1001#0]

=== 2) 敏感工单 SSE（应挂起）===
  最后一个事件: ["review_required", {"ticket_id": "T-API-2", "reason": "命中敏感关键词"}]
  挂起期间新提交 → status=409 code=1005

=== 3) 人工审核通过 ===
  status=200
  data={"review_status":"approved","send_status":"sent",
        "reply":"（人工确认）疑似账号泄露请立即联系安全团队并修改密码。"}

=== 4) 会话查询 ===
  status=200 awaiting_review=False

=== 5) 检索调试接口 ===
  mode=hybrid dense=15 sparse=4 reranked=False
  chunks=['T-1002#0', 'manual-login-module#4', 'manual-login-module#2']

=== 6) 错误场景 ===
  未知会话   → 404 code=1004
  参数非法   → 400 code=1001
  无待审工单 → 409 code=1005
```

---

## 8. 待办 / 注意

- [ ] **首 token 延迟 3.9s / 总耗时 19.8s**：主要耗在本地 `qwen3.5:9b` 的 prefill 与生成。若要走生产，建议换 `qwen3.5:4b` 或加 GPU。
- [ ] **`review` 接口是阻塞的**：`ainvoke` 会等整条链路（含生成）跑完才返回，审核后可能等 20s。后续可改为「提交即返回，结果通过订阅/轮询获取」。
- [ ] **SSE 无断线重连**：事件 id 已生成，但尚未支持 `Last-Event-ID` 续传。
- [ ] **`/ticket/query` 无认证**：内网 MVP 可接受，v1.3 需加 Token。
- [ ] **无并发限制 / 限流**：错误码 3001 已定义但未实现。
- [ ] **`get_graph()` 与 Checkpointer 是进程级单例**：`uvicorn --workers N` 多进程下每个进程各持一份 SQLite 连接，需换 Postgres/Redis。
- [ ] **`api.log` 等本地日志**：已在 `.gitignore` 中忽略，部署时应改为输出到 stdout 由容器收集。
- [ ] **`sse.py` 的 `SSEWriter` 与 `_event_stream` 未单测**：目前通过接口层间接覆盖，后续可补直接单测。

---

*（本文件为编码过程第 08 篇，记录接入层的 SSE 流式实现与两处关键踩坑。）*
