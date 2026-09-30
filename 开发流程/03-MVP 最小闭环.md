# MVP 最小闭环

> 项目名称：企业内部 IT/客服工单智能处理助手
> 文档版本：v1.0
> 文档状态：初稿（待评审）
> 更新日期：2026-09-30
> 上游文档：`01-需求与范围界定.md`、`02-架构与技术选型.md`

---

## 1. MVP 目标

用最小成本跑通 **「工单接入 → 意图识别 → 混合检索 → 草稿生成 → 人工审核 → 发送」** 的端到端闭环，验证以下核心假设：

| 假设 | 验证方式 |
| --- | --- |
| 混合检索能同时命中专有名词与语义提问 | 构造含错误码/版本号 + 语义化提问的测试集，看召回 |
| 本地 Qwen2.5 生成的草稿可用 | 人工评估草稿是否忠实于检索上下文 |
| LangGraph 的 interrupt 能实现审核挂起/恢复 | 敏感工单中断 → 审核接口回调 → 成功恢复发送 |
| 全链路可观测 | LangSmith 中能看到完整 trace |

**成功标准（Demo 级）**：一条真实工单能从提交到「审核后发送」全程跑通，且专有名词类与语义类问题各至少 3 条检索命中。

## 2. MVP 范围界定

### 2.1 做（P0 必须）

1. 单一 HTTP 接口接收工单，SSE 流式返回。
2. 意图识别（简化：`咨询 / 敏感` 二分即可，敏感即走审核）。
3. 混合检索：稠密（qwen3-embedding + Milvus）+ 稀疏（Milvus BM25）→ RRF 融合。
4. 草稿生成：Qwen2.5（Ollama）基于 Top-K 上下文生成。
5. 人工审核：敏感工单 `interrupt` 挂起，提供审核接口恢复。
6. 发送：审核通过后完成发送并落库。
7. 最小知识库：产品手册 + FAQ + 历史工单样例（各若干条）。
8. Docker Compose 一键拉起（api + milvus + etcd + minio + ollama）。

### 2.2 暂不做（P1 延后）

| 暂缓项 | 原因 | 后续版本 |
| --- | --- | --- |
| Qwen3-Reranker 精排 | 先验证召回，再优化精度 | v1.1 |
| 多轮追问 / 长会话记忆 | 先跑通单轮闭环 | v1.1 |
| RAGAS 六指标完整评估 | 先建小评估集，指标后补 | v1.1 |
| 细粒度意图分类（故障/投诉等） | 二分够用 | v1.2 |
| 复杂审核规则引擎（阈值/敏感词表） | 先用关键词命中 | v1.2 |
| 生产级认证 / 多租户 | 内网 Demo 无需 | 后续 |
| 知识库自动增量更新 | 先手动导入 | v1.2 |

> 原则：**MVP 只保「闭环」，不做「完备」**。任何非闭环必需项一律延后。

## 3. 最小闭环流程

```
① 提交工单 (POST /ticket/query, SSE)
        │
        ▼
② 意图识别 ──命中敏感──► ③ 人工审核 (interrupt 挂起)
        │                    │
     普通咨询          POST /ticket/review (approve)
        │                    │
        ▼                    ▼
④ 混合检索 (稠密 + BM25 → RRF) ──► ⑤ 草稿生成 (Qwen2.5, 流式)
        │                              │
        └──────────► ⑥ 发送 ◄──────────┘
                      (落库 + 上报 LangSmith)
```

- 单轮为主；敏感路径通过审核接口恢复图执行。
- 全链路 trace 上报 LangSmith（MVP 仅上报必要字段）。

## 4. 最小功能清单（按开发顺序）

| 序号 | 模块 | 交付内容 | 依赖 |
| --- | --- | --- | --- |
| M1 | 知识库接入 | 文档切分 + qwen3-embedding 向量化 + 写入 Milvus（含 BM25 索引） | Milvus |
| M2 | 混合检索 | 稠密检索 + BM25 检索 + RRF 融合，返回 Top-K | M1 |
| M3 | 生成封装 | Ollama 调用 Qwen2.5，支持流式 | Ollama |
| M4 | 编排图 | LangGraph：意图 → 检索 → 草稿 → (审核) → 发送 | M2、M3 |
| M5 | 接入接口 | FastAPI：`/ticket/query`(SSE)、`/ticket/review` | M4 |
| M6 | 状态持久化 | Checkpointer（挂起/恢复） | M4 |
| M7 | 部署 | Dockerfile + docker-compose | M1~M6 |
| M8 | 观测 | LangSmith 接入 | M4 |

## 5. 最小知识库与数据

| 数据源 | 最小规模 | 格式 | 用途 |
| --- | --- | --- | --- |
| 产品手册 | 3~5 篇（含错误码表、版本号） | MD / PDF | 专有名词检索验证 |
| FAQ | 20~30 条 | MD / CSV | 常见问答 |
| 历史工单 | 20~30 条（含答案） | CSV / JSONL | 语义检索 + 评估集来源 |

- 切分策略：按标题层级切分，chunk ≈ 300~500 token，**重叠 0**（实测 overlap=50 会导致 40% 句子重复，反而污染检索）。
- MVP 采用**手动导入脚本**，不做自动增量。

## 6. 最小评估集

- 构造 **20 条** 测试问题：10 条含专有名词（错误码/版本号），10 条为语义化提问。
- 每条标注：期望命中的知识片段 + 参考答案。
- MVP 只做**人工核对 + 召回命中统计**；RAGAS 六指标留待 v1.1。

## 7. MVP 技术配置（最简）

| 项 | MVP 配置 | 说明 |
| --- | --- | --- |
| 生成模型 | Qwen2.5（7B 或更小，量化） | 本地 Ollama，优先 GPU，无 GPU 用 4bit 量化 |
| Embedding | qwen3-embedding | 同 Milvus 服务 |
| 向量库 | Milvus（单机 compose） | 内置 BM25 |
| 融合 | RRF（k=60） | 免调参 |
| 重排 | 暂不启用 | v1.1 加 Qwen3-Reranker |
| 审核触发 | 敏感关键词命中 | 规则简单可解释 |
| Checkpointer | 内存 / 本地 SQLite | 先本地，后换持久化后端 |
| 观测 | LangSmith（必要字段） | 敏感字段脱敏 |

## 8. 目录结构（MVP 子集）

```
RAG/
├── app/
│   ├── api/ticket.py          # /ticket/query、/ticket/review
│   ├── graph/build.py         # LangGraph 编排图
│   ├── graph/nodes/           # intent / retrieve / draft / review / send
│   ├── retrieval/hybrid.py    # 稠密 + BM25 + RRF
│   ├── generation/ollama.py   # Qwen2.5 流式封装
│   ├── memory/checkpointer.py # 状态持久化
│   ├── schemas/ticket.py      # Pydantic 模型
│   └── core/config.py         # 配置/环境变量
├── ingestion/ingest.py        # 知识库导入脚本
├── evaluation/testset.jsonl   # 20 条最小评估集
├── deploy/
│   ├── Dockerfile
│   └── docker-compose.yml     # api/milvus/etcd/minio/ollama
├── .env.example
└── requirements.txt
```

## 9. 里程碑与任务拆解

| 阶段 | 内容 | 产出 | 预计 |
| --- | --- | --- | --- |
| S1 | 环境就绪：compose 拉起 Milvus/Ollama | 服务可访问 | 0.5 天 |
| S2 | 知识库导入 + 向量化 | M1 完成 | 1 天 |
| S3 | 混合检索 + 20 条测试集验证 | M2 完成，召回报告 | 1.5 天 |
| S4 | 生成封装（流式） | M3 完成 | 0.5 天 |
| S5 | 编排图 + 审核 interrupt | M4/M6 完成 | 2 天 |
| S6 | FastAPI 接口 + SSE | M5 完成 | 1 天 |
| S7 | 端到端联调 + LangSmith | Demo 跑通 | 1 天 |

> 合计约 7.5 个工作日（单人估算，视环境调试浮动）。

## 10. MVP 验收标准（Demo Checklist）

- [ ] `docker-compose up` 可一键拉起全部服务。
- [ ] 知识库成功导入 Milvus，稠密与 BM25 索引均可用。
- [ ] 提交普通工单一：SSE 流式返回草稿，内容忠实于检索上下文。
- [ ] 提交专有名词工单：检索命中对应错误码/版本号知识片段。
- [ ] 提交语义化工单：检索命中语义相关知识片段。
- [ ] 提交敏感工单：触发 `interrupt` 挂起，不直接发送。
- [ ] 调用审核接口 approve 后：成功恢复并发送，状态落库。
- [ ] LangSmith 中可查看该次全链路 trace。
- [ ] 20 条评估集召回命中统计产出。

## 11. 风险与降级方案

| 风险 | 降级方案 |
| --- | --- |
| 本地无 GPU，生成太慢 | 换更小模型 / 4bit 量化 / 缩短上下文；仅 Demo 可接受 |
| Milvus 环境起不来 | 用 Milvus Lite / 临时 FAISS + 简易 BM25 过渡，接口不变 |
| BM25 中文分词效果差 | 引入 jieba 分词配置到 Milvus 稀疏索引 |
| 审核中断恢复异常 | Checkpointer 先落 SQLite，避免内存丢失 |
| 评估集标注耗时 | 先用历史工单答案自动生成初稿，人工粗校 |

## 12. 从 MVP 到完整版的演进

| 版本 | 增量 |
| --- | --- |
| v1.0（MVP） | 单轮闭环 + 混合检索 + 审核中断 |
| v1.1 | + Qwen3-Reranker 精排、多轮追问、RAGAS 六指标 |
| v1.2 | + 细粒度意图、审核规则引擎、知识库增量更新 |
| v1.3 | + 生产级鉴权、监控告警、灰度与 A/B |

---

*（本文件为「MVP 最小闭环」初稿，目标是用最小成本验证核心假设，评审后进入开发。）*
