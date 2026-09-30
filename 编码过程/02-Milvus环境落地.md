# 02-Milvus 环境落地

> 加入日期：2026-09-30
> 关联设计：`开发流程/02-架构与技术选型.md`、`07-部署与运维方案.md`
> 改动文件：`deploy/docker-compose.yml`、`.env.example`、`requirements.txt`、`README.md`、`开发流程/07-部署与运维方案.md`

---

## 1. 在整体布局中的位置（从底层到总体）

Milvus 横跨**两层**：既是第 1 层的**基础设施**（容器运行时），又是第 3 层**检索能力的数据底座**。

```
第 3 层  检索能力        app/retrieval/ (dense/sparse/fuse)   ← 依赖 Milvus
                              │
第 1 层  基础设施/运行时   Milvus v3.0.2  ←── 依赖 ──  etcd (元数据) + MinIO (对象存储)
```

- **依赖谁**：etcd（存 Collection/段元数据）、MinIO（存向量与索引文件）。
- **被谁依赖**：`app/retrieval/*`（检索）、`ingestion/ingest.py`（入库）、`app/api/health.py`（探活）。
- **为什么要有它**：项目要求「稀疏 BM25 + 稠密向量」混合检索，**Milvus 内置 BM25** 让我们不必再额外运维一套 Elasticsearch。

**技术栈：** Docker Desktop 29.7.2 · Docker Compose v5.5.0 · Milvus v3.0.2 · etcd v3.5.25 · MinIO RELEASE.2024-05-28T17-19-04Z · pymilvus 3.0.2

---

## 2. 加入后的项目整体布局

```
RAG/
├── deploy/
│   ├── Dockerfile
│   └── docker-compose.yml      # ← 本次重写：5 个服务 + 版本对齐
├── app/
│   ├── core/config.py          # ← 提供 milvus_host / milvus_port / milvus_uri
│   ├── api/health.py           # ← 探测 Milvus 连通性
│   └── retrieval/              # （待实现）使用 Milvus 做双通道检索
├── ingestion/                  # （待实现）写入 Milvus
├── .env.example                # ← 本次改为「本地直跑」口径
└── requirements.txt            # ← 本次锁定 pymilvus>=3.0,<4
```

**运行态（本次落地）**：

```
it-ticket-assistant-etcd-1    | Up (healthy)
it-ticket-assistant-minio-1   | Up (healthy)
it-ticket-assistant-milvus-1  | Up (healthy)   19530 → 宿主机
```

---

## 3. 本次新增清单

| 项 | 内容 | 说明 |
| --- | --- | --- |
| 镜像 Milvus | `v2.4.15` → **`v3.0.2`** | 与客户端 pymilvus 3.0.2 对齐（跨大版本不兼容） |
| 镜像 etcd | `v3.5.16` → **`v3.5.25`** | 按官方 v3.0.2 standalone compose |
| 镜像 MinIO | 固定 `RELEASE.2024-05-28T17-19-04Z` | 与官方一致 |
| Milvus 环境变量 | 补 **`MINIO_ADDRESS`**、**`MINIO_REGION`** | 原缺失会导致 Milvus 无法连对象存储 |
| Milvus 安全项 | 新增 `security_opt: seccomp:unconfined` | 官方要求 |
| 端口映射 | **移除 `9091:9091`** | 落在 Windows 保留端口段 |
| `api` 服务 | 新增 `MILVUS_HOST` / `OLLAMA_BASE_URL` 覆盖 | 容器内用服务名 |
| `.env.example` | 地址改为 `localhost` | 面向本地直跑，两种运行方式共用一份 `.env` |
| `requirements.txt` | `pymilvus>=3.0,<4` | 避免版本漂移 |

---

## 4. 功能逻辑（按跳转解释）

### 4.1 容器编排与启动顺序

**触发点**：`docker compose -f deploy/docker-compose.yml up -d etcd minio milvus`

```
compose up
   ├─► etcd 启动 ──► healthcheck: etcdctl endpoint health
   ├─► minio 启动 ─► healthcheck: mc ready local
   └─► milvus 启动（depends_on: etcd, minio）
          │
          ├─ 读 ETCD_ENDPOINTS=etcd:2379   → 注册元数据
          ├─ 读 MINIO_ADDRESS=minio:9000   → 挂载对象存储
          └─ healthcheck: curl localhost:9091/healthz
                 └─ 连续成功 → healthy
```

- Milvus 的 `start_period: 90s` 给足冷启动时间，避免被误判为失败。
- `9091`（指标/健康端口）**只在容器内访问**，不映射宿主机 —— 因为该端口落在 Windows 保留段，且外部无需要。

**技术栈：** Docker Compose（`depends_on` / `healthcheck` / `security_opt` / named volumes）

### 4.2 客户端连接

**触发点**：`MilvusClient(uri="http://<host>:19530")`

```
应用启动 / 检索调用
   └─► pymilvus.MilvusClient(uri)
         └─► gRPC 连接 host:19530
               ├─ 连接失败 → 抛出异常 → 检索层降级为仅 BM25（后续实现）
               └─ 连接成功 → 可 list_collections / create_collection / search
```

**技术栈：** pymilvus 3.0.2（gRPC）· Milvus v3.0.2

### 4.3 地址解析（本地直跑 vs 容器内）

**触发点**：任意模块调用 `get_settings()`

```
.env（MILVUS_HOST=localhost）
        │
        ├─ 本地 uvicorn 运行 ──► 直连 localhost:19530  ✅
        └─ compose 内运行 ─────► api 服务 environment 覆盖为 MILVUS_HOST=milvus
                                  └─► 容器网络内解析服务名 milvus  ✅
```

- 一份 `.env` 同时适配两种运行方式，避免「本地改了、容器里又不对」的反复。

**技术栈：** pydantic-settings（env 优先级：进程环境变量 > `.env`）

---

## 5. 关键实现说明

| 项 | 说明 |
| --- | --- |
| 版本对齐依据 | 本机 `pymilvus 3.0.2`；Docker Hub 上 Milvus 稳定 tag 有 `v3.0.0/v3.0.1/v3.0.2`，取 `v3.0.2` 与服务端/客户端一致 |
| 依赖版本来源 | 拉取官方 `milvus-io/milvus` v3.0.2 分支的 `deployments/docker/standalone/docker-compose.yml` 作为参照 |
| 保留端口 | Windows `netsh int ipv4 show excludedportrange protocol=tcp` 显示保留段 `9034-9133`，含 9091；19530 不在其中 |
| 数据卷 | `milvus_data` / `etcd_data` / `minio_data` / `ollama_data` / `checkpoint_data`，均可重建（源文档在仓库） |

---

## 6. 与其他模块的衔接

| 衔接点 | 契约 | 状态 |
| --- | --- | --- |
| `app/core/config.py` → Milvus | `milvus_host` / `milvus_port` / `milvus_uri` | ✅ 已提供 |
| `app/api/health.py` → Milvus | TCP 探测 `host:port`，返回 up/down | ✅ 已验证 |
| `ingestion/ingest.py` → Milvus | Collection `kb_chunks`（`开发流程/04` §2.2） | 🚧 待实现 |
| `app/retrieval/dense.py` → Milvus | 稠密 ANN 检索 | 🚧 待实现 |
| `app/retrieval/sparse.py` → Milvus | 内置 BM25 检索 | 🚧 待实现 |

---

## 7. 验证方式

```bash
# 1) 容器健康
docker compose -f deploy/docker-compose.yml ps

# 2) 客户端连接
python -c "from pymilvus import MilvusClient; print(MilvusClient(uri='http://localhost:19530').list_collections())"

# 3) 应用探活
python -c "from fastapi.testclient import TestClient; from app.main import app; print(TestClient(app).get('/api/v1/health').json())"
```

**本次实测结果：**

| 检查 | 结果 |
| --- | --- |
| etcd / minio / milvus | ✅ 三者均 `Up (healthy)` |
| pymilvus 连接 | ✅ 成功，`collections: []` |
| `/api/v1/health` | ✅ `milvus=up`、`ollama=down`、`checkpointer=up` → `degraded`（ollama 未启动，符合预期） |

---

## 8. 待办 / 注意

- [ ] **ollama 容器未启动**：检索阶段不需要，进入生成阶段前需 `docker compose up -d ollama` 并 `ollama pull qwen2.5:7b`。
- [ ] **Docker Desktop 必须先手动启动**，否则 `docker` 命令报 `dockerDesktopLinuxEngine` 管道不存在。
- [ ] 首次拉取 Milvus 镜像约 2.2GB；命令超过执行时长会被中断，可用后台方式 `Start-Process docker pull ...` 再轮询。
- [ ] 若换了机器，需重新确认 9091 是否仍在保留段内（`netsh`）。
- [ ] 后续为 BM25 配置 `jieba` 中文分词器（Milvus `analyzer_params`）。
- [ ] Milvus 3.x 的 Collection/索引 API 与 2.x 有差异，实现前需以 **pymilvus 3.0.2 文档**为准。

---

*（本文件为编码过程第 02 篇，记录 Milvus 基础环境的落地与踩坑。）*
