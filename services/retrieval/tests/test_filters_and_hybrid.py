"""过滤契约与混合检索测试（用假检索器，不依赖 Milvus / OpenSearch）。"""

from __future__ import annotations

from app.filters import build_filters, extract_doc_ids
from app.hybrid import HybridRetriever
from app.rerank import Reranker

from packages.contracts import ACL, RetrieveMode, SearchHit
from packages.retrievers.base import RETRIEVER_BM25, RETRIEVER_VECTOR


def _hit(chunk_id: str, text: str = "内容", score: float = 1.0, retriever: str = "") -> SearchHit:
    return SearchHit(
        chunk_id=chunk_id,
        doc_id=chunk_id.split(":")[0],
        text=text,
        score=score,
        retriever=retriever,
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

    async def aclose(self) -> None:
        return None


class FailingRetriever:
    name = "failing"

    async def retrieve(self, query: str, top_k: int, filters=None) -> list[SearchHit]:  # noqa: ANN001
        from packages.common.errors import DependencyUnavailable

        raise DependencyUnavailable("OpenSearch", "down")

    async def aclose(self) -> None:
        return None


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
    assert all(
        h.retriever == f"{RETRIEVER_VECTOR}+{RETRIEVER_BM25}" or "+" in h.retriever for h in hits
    )
    assert "fusion" in timings


async def test_hybrid_degrades_when_one_route_fails() -> None:
    vector = FakeRetriever(RETRIEVER_VECTOR, [_hit("d_1:0")])
    hybrid = HybridRetriever(vector, FailingRetriever())
    hits, _ = await hybrid.search(
        "体检报销", RetrieveMode.hybrid, top_k=5, vector_top_k=10, bm25_top_k=10
    )
    assert [h.chunk_id for h in hits] == ["d_1:0"]


# ---------------- 相关性闸门（min_score）----------------
#
# 这些分数取自实测标定（见 docs/adr/0008-relevance-threshold.md）：
#   无关提问（你好/讲笑话/写代码…）  向量 top1  0.3077~0.4077
#   真实制度提问（请假/年假/报销…） 向量 top1  0.4521~0.7785
# BM25 分数量纲是 5~9，永远不会低于 0.43 —— 闸门因此只看向量路。

IRRELEVANT_SCORE = 0.3624  # 「你好」的实测分数
RELEVANT_SCORE = 0.5906  # 「请假什么流程？」的实测分数


async def test_unrelated_query_returns_no_hits_even_when_bm25_matched() -> None:
    """无关提问必须判为「无相关」，且**不能**被 BM25 的字面命中救回来。

    「你好」在向量路被阈值滤空，但 BM25 仍会因字面词匹配带回片段。
    如果这里放行，界面就会给「你好」列出一堆制度切片。
    """
    vector = FakeRetriever(RETRIEVER_VECTOR, [_hit("d_1:0", score=IRRELEVANT_SCORE)])
    bm25 = FakeRetriever(RETRIEVER_BM25, [_hit("d_2:0", score=5.9)])
    hybrid = HybridRetriever(vector, bm25, min_score=0.43)

    hits, _ = await hybrid.search(
        "你好", RetrieveMode.hybrid, top_k=5, vector_top_k=10, bm25_top_k=10
    )
    assert hits == []


async def test_related_query_still_returns_hits() -> None:
    """闸门不能误杀真实提问：向量分过阈值时必须正常召回。"""
    vector = FakeRetriever(RETRIEVER_VECTOR, [_hit("d_1:0", score=RELEVANT_SCORE)])
    bm25 = FakeRetriever(RETRIEVER_BM25, [_hit("d_1:0", score=5.9)])
    hybrid = HybridRetriever(vector, bm25, min_score=0.43)

    hits, _ = await hybrid.search(
        "请假什么流程？", RetrieveMode.hybrid, top_k=5, vector_top_k=10, bm25_top_k=10
    )
    assert [h.chunk_id for h in hits] == ["d_1:0"]


async def test_gate_disabled_keeps_previous_behavior() -> None:
    """min_score=0（未标定/显式关闭）时闸门必须完全放行，行为与改动前一致。"""
    vector = FakeRetriever(RETRIEVER_VECTOR, [_hit("d_1:0", score=IRRELEVANT_SCORE)])
    bm25 = FakeRetriever(RETRIEVER_BM25, [_hit("d_2:0", score=5.9)])
    hybrid = HybridRetriever(vector, bm25, min_score=0.0)

    hits, _ = await hybrid.search(
        "你好", RetrieveMode.hybrid, top_k=5, vector_top_k=10, bm25_top_k=10
    )
    assert hits, "min_score=0 时不应拦截（保留旧行为，便于回退）"


async def test_vector_route_alone_still_cut_by_threshold() -> None:
    """单路 vector 模式也按阈值过滤（这是闸门生效的前提）。"""
    vector = FakeRetriever(RETRIEVER_VECTOR, [_hit("d_1:0", score=IRRELEVANT_SCORE)])
    hybrid = HybridRetriever(vector, FakeRetriever(RETRIEVER_BM25, []), min_score=0.43)

    hits, _ = await hybrid.search(
        "你好", RetrieveMode.vector, top_k=5, vector_top_k=10, bm25_top_k=10
    )
    assert hits == []


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
