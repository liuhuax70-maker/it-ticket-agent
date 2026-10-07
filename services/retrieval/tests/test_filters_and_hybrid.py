"""过滤契约与混合检索测试（用假检索器，不依赖 Milvus / OpenSearch）。"""

from __future__ import annotations

from app.filters import build_filters, extract_doc_ids
from app.hybrid import HybridRetriever
from app.rerank import Reranker

from packages.contracts import ACL, RetrieveMode, SearchHit
from packages.retrievers.base import RETRIEVER_BM25, RETRIEVER_VECTOR


def _hit(chunk_id: str, text: str = "内容", score: float = 1.0, retriever: str = "") -> SearchHit:
    return SearchHit(
        chunk_id=chunk_id, doc_id=chunk_id.split(":")[0], text=text, score=score, retriever=retriever
    )


# ---------------- 过滤契约 ----------------


def test_build_filters_covers_all_visibility_branches() -> None:
    filters = build_filters(ACL(tenant_id="t1", department_id="hr", owner="u_1"))
    assert filters is not None
    assert filters["must"] == {"tenant_id": "t1"}
    clauses = filters["visibility_clauses"]
    assert {"visibility": "public"} in clauses
    assert {"visibility": "department", "department_id": "hr"} in clauses
    assert {"visibility": "private", "owner": "u_1"} in clauses


def test_build_filters_without_owner_omits_private_branch() -> None:
    filters = build_filters(ACL(tenant_id="t1", department_id="hr"))
    assert filters is not None
    assert all(c.get("visibility") != "private" for c in filters["visibility_clauses"])


def test_build_filters_none_acl_returns_none() -> None:
    assert build_filters(None) is None
    assert build_filters(None, doc_ids=["d_1"]) == {"doc_ids": ["d_1"]}


def test_extract_doc_ids() -> None:
    assert extract_doc_ids({"doc_ids": ["d_1", "d_2"]}) == ["d_1", "d_2"]
    assert extract_doc_ids({}) is None
    assert extract_doc_ids(None) is None


# ---------------- 混合检索 ----------------


class FakeRetriever:
    def __init__(self, name: str, hits: list[SearchHit]) -> None:
        self.name = name
        self._hits = hits
        self.calls: list[str] = []

    async def retrieve(self, query: str, top_k: int, filters=None) -> list[SearchHit]:  # noqa: ANN001
        self.calls.append(query)
        return self._hits[:top_k]


class FailingRetriever:
    name = "failing"

    async def retrieve(self, query: str, top_k: int, filters=None) -> list[SearchHit]:  # noqa: ANN001
        from packages.common.errors import DependencyUnavailable

        raise DependencyUnavailable("OpenSearch", "down")


async def test_hybrid_fuses_two_routes_with_rrf() -> None:
    vector = FakeRetriever(RETRIEVER_VECTOR, [_hit("d_1:0"), _hit("d_1:1")])
    bm25 = FakeRetriever(RETRIEVER_BM25, [_hit("d_1:1"), _hit("d_1:0")])
    hybrid = HybridRetriever(vector, bm25, rrf_k=60)

    hits, timings = await hybrid.search(
        "体检报销", RetrieveMode.hybrid, top_k=2, vector_top_k=10, bm25_top_k=10
    )
    assert [h.chunk_id for h in hits] == ["d_1:0", "d_1:1"] or [h.chunk_id for h in hits] == [
        "d_1:1",
        "d_1:0",
    ]
    assert all(h.retriever == f"{RETRIEVER_VECTOR}+{RETRIEVER_BM25}" or "+" in h.retriever for h in hits)
    assert "fusion" in timings


async def test_hybrid_degrades_when_one_route_fails() -> None:
    vector = FakeRetriever(RETRIEVER_VECTOR, [_hit("d_1:0")])
    hybrid = HybridRetriever(vector, FailingRetriever())  # type: ignore[arg-type]
    hits, _ = await hybrid.search(
        "体检报销", RetrieveMode.hybrid, top_k=5, vector_top_k=10, bm25_top_k=10
    )
    assert [h.chunk_id for h in hits] == ["d_1:0"]


async def test_keyword_mode_only_calls_bm25() -> None:
    vector = FakeRetriever(RETRIEVER_VECTOR, [_hit("d_1:0")])
    bm25 = FakeRetriever(RETRIEVER_BM25, [_hit("d_2:0")])
    hybrid = HybridRetriever(vector, bm25)

    hits, _ = await hybrid.search(
        "体检", RetrieveMode.keyword, top_k=3, vector_top_k=10, bm25_top_k=10
    )
    assert [h.chunk_id for h in hits] == ["d_2:0"]
    assert vector.calls == []


# ---------------- 重排 ----------------


async def test_reranker_disabled_is_explicit_passthrough() -> None:
    reranker = Reranker(enabled=False, model_name="unused")
    hits, name, _ = await reranker.rerank("q", [_hit("d_1:0"), _hit("d_1:1")], top_k=1)
    assert name == "rrf"
    assert len(hits) == 1
    assert hits[0].chunk_id == "d_1:0"
