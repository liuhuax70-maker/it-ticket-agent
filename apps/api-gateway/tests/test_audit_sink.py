"""AuditSink（审计落库）的行为测试。

不连真实 Postgres：session_scope 被替换为捕获型的假事务。
重点测三件事：批量落库、丢弃留痕、写库失败不影响调用方。
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

SVC_ROOT = Path(__file__).resolve().parents[1]
if str(SVC_ROOT) not in sys.path:
    sys.path.insert(0, str(SVC_ROOT))

import app.audit_sink as sink_mod  # noqa: E402
from app.audit_sink import AuditSink  # noqa: E402

# client fixture 来自 conftest.py（pytest 自动发现），无需导入


class _FakeSession:
    def __init__(self, store: list) -> None:
        self._store = store
        self.added: list = []

    def add(self, obj) -> None:
        self.added.append(obj)
        self._store.append(obj)


def _fake_session_scope(monkeypatch, store: list) -> dict:
    """替换 session_scope 与 create_all；返回可控的失败开关。"""
    state = {"fail": False}

    @asynccontextmanager
    async def fake_scope(url: str):
        session = _FakeSession(store)
        if state["fail"]:
            raise RuntimeError("db down")
        yield session

    monkeypatch.setattr("app.audit_sink.session_scope", fake_scope)

    async def fake_create_all(url: str) -> None:
        return None

    monkeypatch.setattr("app.audit_sink.create_all", fake_create_all)
    return state


def _record(seq: int) -> dict:
    return {
        "request_id": f"req_{seq}",
        "method": "POST",
        "path": "/chat",
        "status": 200,
        "duration_ms": 10.0,
        "tenant_id": "default",
        "user_id": "u_1",
        "client": "127.0.0.1",
    }


async def _drain_once() -> None:
    # 让事件循环跑到后台任务完成一轮攒批+写库
    for _ in range(6):
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_put_then_batch_write(monkeypatch) -> None:
    store: list = []
    _fake_session_scope(monkeypatch, store)
    sink = AuditSink("postgresql+asyncpg://fake")
    await sink.start()
    try:
        for i in range(3):
            await sink.put(_record(i))
        await _drain_once()
    finally:
        await sink.aclose()
    assert len(store) == 3
    assert all(type(obj).__name__ == "AuditLog" for obj in store)
    assert store[0].request_id == "req_0"
    assert sink.dropped == 0


@pytest.mark.asyncio
async def test_write_failure_does_not_raise_and_is_counted(monkeypatch) -> None:
    store: list = []
    state = _fake_session_scope(monkeypatch, store)
    state["fail"] = True
    sink = AuditSink("postgresql+asyncpg://fake")
    await sink.start()
    try:
        await sink.put(_record(1))
        await _drain_once()
    finally:
        await sink.aclose()
    assert sink.dropped == 1, "写库失败必须被计数留痕"
    assert store == []


@pytest.mark.asyncio
async def test_queue_full_drops_and_counts(monkeypatch) -> None:
    store: list = []
    _fake_session_scope(monkeypatch, store)
    monkeypatch.setattr(sink_mod, "_QUEUE_MAX", 2)
    sink = AuditSink("postgresql+asyncpg://fake")
    await sink.start()
    try:
        # 不启动前先灌满队列：put 5 条，容量 2 -> 丢 3
        for i in range(5):
            await sink.put(_record(i))
    finally:
        await sink.aclose()
    assert sink.dropped == 3


def test_admin_audit_endpoint_reports_disabled(client) -> None:
    """sink 未启用时返回空列表+说明，而不是 500——管理面保持可浏览。"""
    resp = client.get("/admin/audit")
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert "未启用" in body["note"]


def test_middleware_feeds_the_sink(client, monkeypatch) -> None:
    """审计中间件 -> sink 的接线：发一个请求，sink 应收到一条 /chat 记录。"""
    # lifespan 已把 audit_sink 置 None（测试配置禁用了落库）；注入一个假 sink 验证接线
    received: list[dict] = []

    class _FakeSink:
        async def put(self, record: dict) -> None:
            received.append(record)

    from fastapi.testclient import TestClient as _TC  # noqa: F401 - 说明：沿用 client 的 app

    app = client.app
    app.state.audit_sink = _FakeSink()
    try:
        resp = client.post("/chat", json={"query": "问题", "top_k": 3})
        assert resp.status_code == 200
    finally:
        app.state.audit_sink = None
    assert len(received) == 1
    rec = received[0]
    assert rec["path"] == "/chat"
    assert rec["status"] == 200
    assert rec["user_id"], "身份字段必须来自 IdentityMiddleware"
