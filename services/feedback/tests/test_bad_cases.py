"""坏例采集/导出与反馈接口测试（用假存储，不依赖 Postgres）。"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.bad_cases import BadCaseCollector

import app.main as main_module


class FakeStore:
    def __init__(self, url: str = "") -> None:  # noqa: ARG002
        self.rows: list[dict] = []
        self.added: list[dict] = []

    async def add(self, **kwargs) -> str:  # noqa: ANN003
        self.added.append(kwargs)
        return "f_1"

    async def list_recent(self, *, tenant_id=None, limit: int = 50) -> list[dict]:  # noqa: ANN001, ARG002
        return self.rows[:limit]

    async def list_bad_cases(self, *, limit: int = 100, max_rating: int = -1, tenant_id=None) -> list[dict]:  # noqa: ANN001, ARG002
        return [row for row in self.rows if row["rating"] is not None and row["rating"] <= max_rating][:limit]

    async def health(self) -> tuple[bool, str]:
        return True, "fake"


def _bad_case(query: str) -> dict:
    return {
        "id": "f_1",
        "tenant_id": "default",
        "user_id": "u_secret",
        "query": query,
        "answer": "错误的答案",
        "rating": -1,
        "comment": "答案与文档不符",
        "trace_id": "tr_1",
        "created_at": "2026-10-07T00:00:00+00:00",
    }


async def test_collect_returns_only_negative_feedback(tmp_path) -> None:
    store = FakeStore()
    store.rows = [_bad_case("q1"), {**_bad_case("q2"), "rating": 1}]
    collector = BadCaseCollector(store, str(tmp_path))  # type: ignore[arg-type]

    cases = await collector.collect()
    assert [c["query"] for c in cases] == ["q1"]


async def test_export_writes_jsonl_without_user_id(tmp_path) -> None:
    store = FakeStore()
    store.rows = [_bad_case("q1")]
    collector = BadCaseCollector(store, str(tmp_path))  # type: ignore[arg-type]

    path = await collector.export()
    assert path.exists()
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["question"] == "q1"
    assert lines[0]["source"] == "user_feedback"
    assert "user_id" not in lines[0], "数据集不应携带用户标识"


def test_feedback_endpoint_stores_payload(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "FeedbackStore", FakeStore)
    with TestClient(main_module.app) as client:
        resp = client.post(
            "/feedback",
            json={"query": "q", "answer": "a", "rating": -1, "comment": "不准"},
            headers={"x-tenant-id": "t1", "x-user-id": "u1"},
        )
        assert resp.status_code == 200
        assert resp.json()["id"] == "f_1"

        store = client.app.state.store  # type: ignore[attr-defined]
        assert store.added[0]["tenant_id"] == "t1"
        assert store.added[0]["rating"] == -1


def test_health_reports_postgres(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "FeedbackStore", FakeStore)
    with TestClient(main_module.app) as client:
        body = client.get("/health").json()
    assert body["status"] == "ok"
    assert "postgres" in body["details"]
