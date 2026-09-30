"""接入层测试。

检索与生成全部 mock，Checkpointer 换成内存实现，
因此可以完整验证 SSE 事件契约与审核接口，而不依赖 Milvus / Ollama。
"""

import json

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from app.api import session as session_api
from app.api import ticket as ticket_api
from app.graph import build
from app.graph.nodes import draft as draft_module
from app.graph.nodes import retrieve as retrieve_module
from app.main import app
from app.schemas.retrieval import Chunk, DocSource, RetrievalMode


def _chunk(chunk_id: str = "doc#0") -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        doc_id="doc",
        content="ERR-4041 表示令牌已过期，请重新登录。",
        source=DocSource.MANUAL,
        title="常见错误码",
    )


def _debug() -> dict:
    return {
        "mode": "hybrid",
        "dense_hits": 1,
        "sparse_hits": 1,
        "fused": 1,
        "reranked": False,
        "dense_error": None,
        "sparse_error": None,
        "top": [],
    }


def _parse_events(body: str) -> list[tuple[str, dict]]:
    """把 SSE 文本解析成 (event, data) 列表。"""
    events: list[tuple[str, dict]] = []
    for block in body.strip().split("\n\n"):
        if not block.strip():
            continue
        event_name = None
        data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                event_name = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        if event_name:
            events.append((event_name, data or {}))
    return events


@pytest.fixture
def client(monkeypatch):
    """构造一个用内存 Checkpointer + mock 检索/生成的测试客户端。"""
    monkeypatch.setattr(
        retrieve_module,
        "hybrid_search",
        lambda query, **kwargs: ([_chunk()], RetrievalMode.HYBRID, _debug()),
    )
    monkeypatch.setattr(draft_module, "generate", lambda system, user: "草稿内容 [来源: doc#0]")
    monkeypatch.setattr(draft_module, "generate_stream", lambda system, user: iter(["草稿", "内容"]))

    graph = build.build_graph(checkpointer=InMemorySaver())

    async def _fake_get_graph():
        return graph

    monkeypatch.setattr(ticket_api, "get_graph", _fake_get_graph)
    monkeypatch.setattr(session_api, "get_graph", _fake_get_graph)

    return TestClient(app)


def _query(client: TestClient, session_id: str, query: str, ticket_id: str = "T-1"):
    with client.stream(
        "POST",
        "/api/v1/ticket/query",
        json={"ticket_id": ticket_id, "session_id": session_id, "query": query},
    ) as response:
        return response, "".join(response.iter_text())


# ---------------- SSE：普通工单 ----------------


def test_sse_normal_ticket_event_sequence(client):
    response, body = _query(client, "s-normal", "ERR-4041 怎么解决")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers.get("X-Trace-Id")

    events = _parse_events(body)
    names = [name for name, _ in events]

    assert names[0] == "start"
    assert "intent" in names
    assert "retrieval" in names
    assert "token" in names
    assert "draft" in names
    assert names[-1] == "done"

    payload = dict(events)
    assert payload["intent"]["need_review"] is False
    assert payload["retrieval"]["hit_count"] == 1
    assert payload["done"]["send_status"] == "sent"
    # token 应能拼回草稿
    tokens = "".join(data["delta"] for name, data in events if name == "token")
    assert tokens == payload["draft"]["draft"]


# ---------------- SSE：敏感工单 ----------------


def test_sse_sensitive_ticket_ends_with_review_required(client):
    response, body = _query(client, "s-sensitive", "我要投诉登录问题", ticket_id="T-2")

    events = _parse_events(body)
    names = [name for name, _ in events]

    assert "review_required" in names
    assert names[-1] == "review_required"  # 挂起后流即结束
    assert "done" not in names

    payload = dict(events)
    assert payload["intent"]["need_review"] is True
    assert payload["review_required"]["reason"] == "命中敏感关键词"


def test_sse_blocks_new_query_while_awaiting_review(client):
    _query(client, "s-blocked", "我要投诉登录问题", ticket_id="T-3")

    # 该场景会被提前拦下并返回普通 JSON 错误（不是 SSE），因此用普通 POST
    response = client.post(
        "/api/v1/ticket/query",
        json={"ticket_id": "T-4", "session_id": "s-blocked", "query": "另一个问题"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == 1005


# ---------------- 审核接口 ----------------


def test_review_approved_resumes_and_sends(client):
    _query(client, "s-approve", "我要投诉登录问题", ticket_id="T-5")

    response = client.post(
        "/api/v1/ticket/review",
        json={
            "ticket_id": "T-5",
            "session_id": "s-approve",
            "decision": "approved",
            "comment": "可以发",
        },
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["review_status"] == "approved"
    assert data["send_status"] == "sent"
    assert data["reply"]


def test_review_rejected_does_not_send(client):
    _query(client, "s-reject", "我要投诉登录问题", ticket_id="T-6")

    response = client.post(
        "/api/v1/ticket/review",
        json={"ticket_id": "T-6", "session_id": "s-reject", "decision": "rejected"},
    )

    data = response.json()["data"]
    assert data["review_status"] == "rejected"
    assert data["send_status"] is None
    assert not data["reply"]


def test_review_without_pending_ticket_returns_conflict(client):
    _query(client, "s-done", "ERR-4041 怎么解决", ticket_id="T-7")  # 普通工单，无需审核

    response = client.post(
        "/api/v1/ticket/review",
        json={"ticket_id": "T-7", "session_id": "s-done", "decision": "approved"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == 1005


def test_review_with_mismatched_ticket_id_returns_conflict(client):
    _query(client, "s-mismatch", "我要投诉登录问题", ticket_id="T-8")

    response = client.post(
        "/api/v1/ticket/review",
        json={"ticket_id": "T-OTHER", "session_id": "s-mismatch", "decision": "approved"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == 1005


# ---------------- 会话查询 ----------------


def test_session_query_returns_ticket(client):
    _query(client, "s-session", "ERR-4041 怎么解决", ticket_id="T-9")

    response = client.get("/api/v1/session/s-session")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["awaiting_review"] is False
    assert data["tickets"][0]["ticket_id"] == "T-9"
    assert data["tickets"][0]["status"] == "sent"


def test_session_query_marks_awaiting_review(client):
    _query(client, "s-wait", "我要投诉登录问题", ticket_id="T-10")

    data = client.get("/api/v1/session/s-wait").json()["data"]

    assert data["awaiting_review"] is True
    assert data["tickets"][0]["status"] == "awaiting_review"


def test_session_query_unknown_session_returns_not_found(client):
    response = client.get("/api/v1/session/does-not-exist")

    assert response.status_code == 404
    assert response.json()["code"] == 1004


# ---------------- 检索调试接口 ----------------


def test_retrieval_search_returns_mapped_response(client, monkeypatch):
    async def fake_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr("app.api.retrieval.asyncio.to_thread", fake_to_thread)
    # 该模块按名字导入了 hybrid_search，需就地替换（否则会真的去连 Milvus）
    monkeypatch.setattr(
        "app.api.retrieval.hybrid_search",
        lambda query, *args: ([_chunk()], RetrievalMode.HYBRID, _debug()),
    )

    response = client.post("/api/v1/retrieval/search", json={"query": "ERR-4041", "top_k": 3})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["mode"] == "hybrid"
    assert data["dense_hits"] == 1
    assert len(data["chunks"]) == 1
    assert data["chunks"][0]["chunk_id"] == "doc#0"


# ---------------- 参数校验 ----------------


def test_query_validation_error_returns_business_code(client):
    response = client.post(
        "/api/v1/ticket/query", json={"ticket_id": "T-1", "session_id": "s", "query": ""}
    )

    assert response.status_code == 400
    assert response.json()["code"] == 1001


def test_review_rejects_unknown_decision(client):
    response = client.post(
        "/api/v1/ticket/review",
        json={"ticket_id": "T-1", "session_id": "s", "decision": "maybe"},
    )

    assert response.status_code == 400
    assert response.json()["code"] == 1001
