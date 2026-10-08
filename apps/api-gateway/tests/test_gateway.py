"""api-gateway 集成式测试（TestClient + 假下游，不依赖任何外部服务）。"""

from __future__ import annotations

import app.main as main_module
from app.config import Settings
from fastapi.testclient import TestClient

from packages.contracts import (
    ChatRequest,
    ChatResponse,
    Citation,
    FeedbackRequest,
    FeedbackResponse,
    IngestRequest,
    IngestResponse,
    ModelInfo,
)


class FakeCounter:
    """替代 RedisCounter：内存计数，测试可重复运行。"""

    def __init__(self, url: str) -> None:  # noqa: ARG002
        self.counts: dict[str, int] = {}

    async def incr(self, key: str, ttl_seconds: int) -> int:  # noqa: ARG002
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def get(self, key: str) -> int:
        return self.counts.get(key, 0)

    async def aclose(self) -> None:
        return None


class FakeOrchestrator:
    def __init__(self) -> None:
        self.calls: list[tuple[ChatRequest, object]] = []

    async def chat(self, req: ChatRequest, identity) -> ChatResponse:  # noqa: ANN001
        self.calls.append((req, identity))
        return ChatResponse(
            answer="转正后凭发票报销，上限五百元[1]。",
            citations=[
                Citation(
                    index=1,
                    chunk_id="d_1:4",
                    doc_id="d_1",
                    doc_title="员工手册",
                    chunk_index=4,
                    section_path="第三章 福利 > 3.2 入职体检",
                    char_start=120,
                    char_end=180,
                    snippet="转正后凭发票报销，上限五百元。",
                )
            ],
            timings_ms={"retrieve": 3.0, "generate": 12.0, "total": 20.0},
            model="deepseek-chat",
            trace_id="tr_1",
        )

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


class FakeIngestion:
    """记录调用入参，便于断言「网关有没有把该传的东西传对」。"""

    def __init__(self) -> None:
        self.ingest_requests: list[IngestRequest] = []
        self.list_query: dict = {}

    async def stats(self) -> dict:
        return {"documents": 1, "chunks": 6}

    async def list_documents(
        self, *, tenant_id=None, keyword=None, limit: int = 50, offset: int = 0
    ) -> dict:  # noqa: ANN001
        self.list_query = {
            "tenant_id": tenant_id,
            "keyword": keyword,
            "limit": limit,
            "offset": offset,
        }
        return {
            "total": 1,
            "limit": limit,
            "offset": offset,
            "items": [
                {
                    "doc_id": "d_1",
                    "title": "员工手册",
                    "source": "data/corpus/employee_handbook.md",
                    "status": "indexed",
                    "chunk_count": 10,
                    "size_bytes": 2345,
                    "tenant_id": tenant_id,
                    "department_id": "default",
                    "visibility": "internal",
                    "created_at": None,
                    "updated_at": None,
                }
            ],
        }

    async def get_document(self, doc_id: str) -> dict:
        return {"doc_id": doc_id, "status": "indexed"}

    async def delete_document(self, doc_id: str) -> dict:
        return {"doc_id": doc_id, "deleted": {"milvus": 1, "opensearch": 1}, "existed": True}

    async def ingest(self, req: IngestRequest) -> IngestResponse:
        self.ingest_requests.append(req)
        return IngestResponse(
            job_id="j1", status="succeeded", documents=1, chunk_count=6, indexed=6
        )

    async def upload(
        self, filename: str, content: bytes, *, acl, reindex: bool = False
    ) -> IngestResponse:  # noqa: ANN001, ARG002
        return IngestResponse(
            job_id="j2", status="succeeded", documents=1, chunk_count=2, indexed=2
        )

    async def get_job(self, job_id: str) -> dict:
        return {"job_id": job_id, "status": "succeeded"}

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


class FakeModelGateway:
    async def models(self) -> list[ModelInfo]:
        return [ModelInfo(name="deepseek-chat", provider="deepseek", note="当前生效")]

    async def quota(self, tenant_id: str) -> dict:
        return {"tenant_id": tenant_id, "used_today": 10, "daily_limit": 100, "enforced": False}

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


class FakeFeedback:
    def __init__(self) -> None:
        self.received: list[FeedbackRequest] = []

    async def submit(self, req: FeedbackRequest) -> FeedbackResponse:
        self.received.append(req)
        return FeedbackResponse(id="f_1")

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


def _build_app(monkeypatch, **overrides):
    monkeypatch.setattr(main_module, "RedisCounter", FakeCounter)
    defaults = dict(
        rate_limit_enabled=True,
        rate_limit_per_minute=3,
        serve_ui=False,
        audit_enabled=True,
        # 测试不连真实 Postgres：audit_database_url 为空 = 仅 stdout 审计
        audit_database_url="",
        authz_enabled=False,
        cors_origins="",
    )
    defaults.update(overrides)
    app = main_module.create_app(Settings(**defaults))
    return app


# ---------------- chat ----------------


def test_chat_returns_answer_with_citations(client) -> None:
    resp = client.post("/chat", json={"query": "入职体检费用怎么报销？"})
    assert resp.status_code == 200
    body = resp.json()
    assert "五百元" in body["answer"]
    assert body["citations"][0]["chunk_id"] == "d_1:4"
    assert body["timings_ms"]["total"] == 20.0


def test_identity_is_forwarded_downstream(client) -> None:
    client.post("/chat", json={"query": "x"})
    app = client.app
    _, identity = app.state.orchestrator.calls[0]
    assert identity.tenant_id == "default"  # 鉴权关闭时使用默认身份
    assert identity.user_id == "u_demo"


def test_request_id_header_is_returned(client) -> None:
    resp = client.post("/chat", json={"query": "x"})
    assert resp.headers["x-request-id"]


def test_invalid_body_is_422(client) -> None:
    assert client.post("/chat", json={}).status_code == 422


def test_chat_models_lists_models_for_chat_users(client) -> None:
    """模型清单挂在 /chat 下（chat 权限），普通聊天用户能直接拿到下拉选项。

    刻意不走 /admin/models：那条要 rag_admin 角色，普通用户会 403，
    前端拿不到清单就静默退化成"只有一个模型"，用户无从判断原因。
    """
    resp = client.get("/chat/models")
    assert resp.status_code == 200
    body = resp.json()
    assert [m["name"] for m in body] == ["deepseek-chat"]
    assert body[0]["kind"] == "chat"


def test_chat_models_rejects_user_without_chat_permission(monkeypatch) -> None:
    """开启了鉴权时，无 token 必须被拦下——清单不能绕过 chat 权限。"""
    app = _build_app(monkeypatch, authz_enabled=True, keycloak_url="http://localhost:8180")
    with TestClient(app) as test_client:
        resp = test_client.get("/chat/models")
    assert resp.status_code == 401


def test_exempt_root_does_not_open_the_rest(monkeypatch) -> None:
    """放行根路径不能变成"全放行"。

    中间件按 ``path == item or path.startswith(item + "/")`` 匹配豁免项，
    item 为 "/" 时前缀是 "//"——只有精确的 "/" 命中，/chat 之类仍必须 401。
    这条测试守的就是这个前提：一旦有人把匹配改成裸 startswith(item)，
    整个网关会静默失去鉴权，而所有"未登录返回 401"的测试仍然通过。
    """
    app = _build_app(monkeypatch, authz_enabled=True, keycloak_url="http://localhost:8180")
    with TestClient(app) as test_client:
        # serve_ui=False 时根路径没有注册路由：豁免生效的话是 404，而不是被拦成 401
        assert test_client.get("/").status_code == 404
        assert test_client.post("/chat", json={"query": "x"}).status_code == 401
        assert test_client.get("/chat/models").status_code == 401


def test_root_redirects_to_ui_when_served(monkeypatch) -> None:
    """开启 UI 时，根路径必须能走到登录页（这是修复前真实存在的死锁）。"""
    app = _build_app(
        monkeypatch, authz_enabled=True, keycloak_url="http://localhost:8180", serve_ui=True
    )
    with TestClient(app) as test_client:
        root = test_client.get("/", follow_redirects=False)
        ui = test_client.get("/ui/", follow_redirects=False)
    assert root.status_code in (302, 307)
    assert root.headers["location"] == "/ui/"
    # 登录页本身必须能匿名访问，否则还是到不了
    assert ui.status_code == 200


def test_chat_forwards_selected_model_downstream(client) -> None:
    """选了模型就必须原样传到编排层，否则下拉是个摆设。"""
    client.post("/chat", json={"query": "x", "model": "deepseek-chat"})
    req, _ = client.app.state.orchestrator.calls[0]
    assert req.model == "deepseek-chat"


def test_chat_without_model_leaves_it_unset(client) -> None:
    """留空表示"由网关挑默认模型"，不能塞一个具体模型名下去。

    塞了就等于绕过 model-gateway 的兜底链（首个模型不可用时本该自动降级）。
    """
    client.post("/chat", json={"query": "x"})
    req, _ = client.app.state.orchestrator.calls[0]
    assert req.model is None


# ---------------- 限流 ----------------


def test_rate_limit_blocks_after_threshold(client) -> None:
    for _ in range(3):
        assert client.post("/chat", json={"query": "q"}).status_code == 200
    blocked = client.post("/chat", json={"query": "q"})
    assert blocked.status_code == 429
    assert "retry-after" in blocked.headers


def test_health_is_exempt_from_rate_limit(client) -> None:
    for _ in range(6):
        assert client.get("/health").status_code == 200


# ---------------- documents / admin / feedback ----------------


def test_documents_ingest_and_delete(client) -> None:
    resp = client.post("/documents/ingest", json={"path": "data/corpus"})
    assert resp.status_code == 200
    assert resp.json()["chunk_count"] == 6

    deleted = client.delete("/documents/d_1")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"]["milvus"] == 1


def test_documents_list_is_scoped_to_identity_tenant(client) -> None:
    resp = client.get("/documents", params={"keyword": "手册"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["doc_id"] == "d_1"

    query = client.app.state.ingestion.list_query
    # 租户必须来自身份，不接受客户端传参
    assert query["tenant_id"] == "default"
    assert query["keyword"] == "手册"


def test_ingest_cannot_override_tenant_or_department(client) -> None:
    """越权写入防护：客户端只能选可见范围，租户/部门一律由身份决定。"""
    resp = client.post(
        "/documents/ingest",
        json={
            "content": "# x",
            "filename": "a.md",
            "acl": {
                "tenant_id": "other-tenant",
                "department_id": "other-dept",
                "visibility": "department",
            },
        },
    )
    assert resp.status_code == 200
    req = client.app.state.ingestion.ingest_requests[-1]
    assert req.acl is not None
    assert req.acl.tenant_id == "default"
    assert req.acl.department_id == "default"
    assert req.acl.visibility.value == "department"


def test_admin_stats_requires_authz_when_enabled(client) -> None:
    assert client.get("/admin/stats").status_code == 200
    assert client.get("/admin/models").json()[0]["name"] == "deepseek-chat"
    assert client.get("/admin/quotas/default").json()["used_today"] == 10


def test_feedback_is_forwarded(client) -> None:
    resp = client.post("/feedback", json={"query": "q", "answer": "a", "rating": 1})
    assert resp.status_code == 200
    assert resp.json()["id"] == "f_1"
    app = client.app
    assert app.state.feedback.received[0].rating == 1


# ---------------- 鉴权开关 ----------------


def test_authz_enabled_rejects_missing_token(monkeypatch) -> None:
    app = _build_app(monkeypatch, authz_enabled=True, keycloak_url="http://localhost:8180")
    with TestClient(app) as test_client:
        resp = test_client.post("/chat", json={"query": "x"})
    assert resp.status_code == 401
