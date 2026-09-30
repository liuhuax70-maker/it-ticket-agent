# 企业内部 IT/客服工单智能处理助手

员工或客户提交 IT/产品问题 → 自动检索内部知识库 → 生成回复草稿 → 复杂/敏感工单转人工审核 → 审核后发送。

## 技术栈

| 环节 | 技术 |
| --- | --- |
| 接口 | FastAPI + SSE 流式 |
| 编排 | LangGraph（意图识别 → 检索 → 草稿 → 人工审核 → 发送） |
| 组件 | LangChain（Tools / Memory / Middleware） |
| 生成 | Qwen2.5（Ollama，本地） |
| 检索 | qwen3-embedding + Milvus 内置 BM25 + RRF + Qwen3-Reranker |
| 评估 | RAGAS 六指标 |
| 可观测 | LangSmith |
| 部署 | Docker + docker-compose |

## 项目结构

```
RAG/
├── app/
│   ├── main.py            # FastAPI 入口（trace_id 中间件 + 异常处理器）
│   ├── api/               # 接入层：health / ticket / session / retrieval
│   ├── core/              # 配置、日志、错误码
│   ├── schemas/           # Pydantic 数据契约（唯一事实来源）
│   ├── graph/             # LangGraph 编排（state / build / nodes）
│   ├── retrieval/         # 稠密 + BM25 + RRF + 重排
│   ├── generation/        # Qwen2.5（Ollama）与 Prompt 模板
│   └── memory/            # Checkpointer（会话状态）
├── ingestion/             # 知识库切分与入库
├── evaluation/            # RAGAS 评估集与脚本
├── deploy/                # Dockerfile + docker-compose.yml
├── tests/                 # 单元测试
├── 开发流程/               # 事前设计文档（01~07）
└── 编码过程/               # 编码过程记录（每个框架/功能一篇）
```

## 快速开始

### 方式一：Docker Compose（推荐）

```bash
cp .env.example .env          # 按需填写
make up                       # 构建并启动全部服务
make pull-model               # 拉取 Qwen2.5
curl http://localhost:8000/api/v1/health
```

### 方式二：本地直跑（仅 API）

```bash
pip install -r requirements.txt
# 本地运行时把 .env 中的 MILVUS_HOST 改为 localhost、OLLAMA_BASE_URL 改为 http://localhost:11434
cp .env.example .env
uvicorn app.main:app --reload
```

- 接口文档：http://localhost:8000/docs
- 健康检查：http://localhost:8000/api/v1/health

### 运行测试

```bash
pytest -q
```

## 接口一览（`/api/v1`）

| 接口 | 方法 | 状态 |
| --- | --- | --- |
| `/health` | GET | 已实现 |
| `/ticket/query` | POST (SSE) | 骨架占位 |
| `/ticket/review` | POST | 骨架占位 |
| `/session/{session_id}` | GET | 骨架占位 |
| `/retrieval/search` | POST | 骨架占位 |

契约详见 `开发流程/05-接口与数据契约设计.md`。

## 文档

- 需求与架构：`开发流程/01-需求与范围界定.md` ~ `03-MVP 最小闭环.md`
- 设计细节：`开发流程/04-检索与编排设计.md` ~ `05-接口与数据契约设计.md`
- 测试与部署：`开发流程/06-测试与验收方案.md` ~ `07-部署与运维方案.md`
- 编码过程：`编码过程/`
