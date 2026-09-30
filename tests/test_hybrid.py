"""混合检索编排与降级的单元测试。

两个通道用 monkeypatch 替换，因此**不依赖 Milvus / Ollama**，
可以稳定覆盖「任一路失败/超时」等真实环境里很难复现的分支。
"""

import time

from app.retrieval import dense, hybrid, sparse
from app.schemas.retrieval import Chunk, DocSource, RetrievalFilters, RetrievalMode


def _chunk(cid: str) -> Chunk:
    return Chunk(
        chunk_id=cid,
        doc_id=cid.split("#")[0],
        content=f"内容 {cid}",
        source=DocSource.FAQ,
    )


# ---------------- 正常路径 ----------------


def test_hybrid_fuses_both_channels(monkeypatch):
    """两路都命中时走 HYBRID，且被两路同时命中的片段应排第一。"""
    monkeypatch.setattr(dense, "search_dense", lambda q, n, f=None: [_chunk("a#1"), _chunk("b#1")])
    monkeypatch.setattr(sparse, "search_sparse", lambda q, n, f=None: [_chunk("b#1"), _chunk("c#1")])

    chunks, mode, debug = hybrid.hybrid_search("登录报错", top_k=3, rerank_enabled=False)

    assert mode is RetrievalMode.HYBRID
    assert debug["dense_hits"] == 2
    assert debug["sparse_hits"] == 2
    assert debug["fused"] == 3
    assert chunks[0].chunk_id == "b#1"  # 两路均命中 → RRF 最高
    assert {c.chunk_id for c in chunks} == {"a#1", "b#1", "c#1"}


def test_hybrid_truncates_to_top_k(monkeypatch):
    many = [_chunk(f"a#{i}") for i in range(10)]
    monkeypatch.setattr(dense, "search_dense", lambda q, n, f=None: many)
    monkeypatch.setattr(sparse, "search_sparse", lambda q, n, f=None: [])

    chunks, _, debug = hybrid.hybrid_search("q", top_k=3, rerank_enabled=False)

    assert len(chunks) == 3
    assert len(debug["top"]) == 3


def test_hybrid_passes_filters_to_both_channels(monkeypatch):
    captured = {}

    def fake_dense(query, top_n, filters=None):
        captured["dense"] = filters
        return []

    def fake_sparse(query, top_n, filters=None):
        captured["sparse"] = filters
        return []

    monkeypatch.setattr(dense, "search_dense", fake_dense)
    monkeypatch.setattr(sparse, "search_sparse", fake_sparse)

    filters = RetrievalFilters(source=DocSource.MANUAL)
    hybrid.hybrid_search("q", filters=filters)

    assert captured["dense"] is filters
    assert captured["sparse"] is filters


def test_hybrid_uses_configured_top_k_by_default(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(dense, "search_dense", lambda q, n, f=None: [_chunk(f"a#{i}") for i in range(9)])
    monkeypatch.setattr(sparse, "search_sparse", lambda q, n, f=None: [])

    chunks, _, _ = hybrid.hybrid_search("q")

    assert len(chunks) == get_settings().top_k


# ---------------- 查询期去重 ----------------


def test_dedupe_chunks_removes_exact_duplicates():
    from app.retrieval.hybrid import dedupe_chunks

    same = _chunk("a#1")
    other = _chunk("b#1")
    other.content = "完全不同的内容" + "补充文字"

    result = dedupe_chunks([same, same.model_copy(), other])

    assert [c.chunk_id for c in result] == ["a#1", "b#1"]


def test_dedupe_chunks_removes_near_duplicates_by_prefix():
    """不同来源但开头高度一致（转载/重复收录）应被去掉。

    近重复判定看归一化后的前 120 字，因此测试前缀必须超过该长度。
    """
    from app.retrieval.hybrid import dedupe_chunks

    prefix = "这是一段被多个来源重复收录的长文本" * 10
    first = _chunk("a#1")
    first.content = prefix + "甲"
    second = _chunk("b#1")
    second.content = prefix + "乙"

    result = dedupe_chunks([first, second])

    assert len(result) == 1


def test_hybrid_search_reports_dedupe_in_debug(monkeypatch):
    prefix = "重复开头" * 40
    dup_a = _chunk("a#1")
    dup_a.content = prefix + "甲"
    dup_b = _chunk("b#1")
    dup_b.content = prefix + "乙"

    monkeypatch.setattr(dense, "search_dense", lambda q, n, f=None: [dup_a, dup_b])
    monkeypatch.setattr(sparse, "search_sparse", lambda q, n, f=None: [])

    _, _, debug = hybrid.hybrid_search("q", rerank_enabled=False)

    assert debug["fused_before_dedupe"] == 2
    assert debug["fused"] == 1


# ---------------- 降级路径 ----------------


def test_hybrid_degrades_to_sparse_when_dense_fails(monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("milvus down")

    monkeypatch.setattr(dense, "search_dense", boom)
    monkeypatch.setattr(sparse, "search_sparse", lambda q, n, f=None: [_chunk("s#1")])

    chunks, mode, debug = hybrid.hybrid_search("q", rerank_enabled=False)

    assert mode is RetrievalMode.SPARSE_ONLY
    assert [c.chunk_id for c in chunks] == ["s#1"]
    assert "milvus down" in debug["dense_error"]
    assert debug["sparse_error"] is None


def test_hybrid_degrades_to_dense_when_sparse_fails(monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("bm25 broken")

    monkeypatch.setattr(dense, "search_dense", lambda q, n, f=None: [_chunk("d#1")])
    monkeypatch.setattr(sparse, "search_sparse", boom)

    chunks, mode, debug = hybrid.hybrid_search("q", rerank_enabled=False)

    assert mode is RetrievalMode.DENSE_ONLY
    assert [c.chunk_id for c in chunks] == ["d#1"]
    assert "bm25 broken" in debug["sparse_error"]


def test_hybrid_returns_degraded_when_both_channels_fail(monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("everything down")

    monkeypatch.setattr(dense, "search_dense", boom)
    monkeypatch.setattr(sparse, "search_sparse", boom)

    chunks, mode, debug = hybrid.hybrid_search("q", rerank_enabled=False)

    assert mode is RetrievalMode.DEGRADED
    assert chunks == []
    assert debug["top"] == []
    assert debug["dense_error"] and debug["sparse_error"]


def test_hybrid_degrades_channel_on_timeout(monkeypatch):
    """单通道超时应只降级该通道，另一通道照常返回。"""

    class FakeSettings:
        top_k = 5
        top_n_dense = 5
        top_n_sparse = 5
        top_n_fused = 10
        rrf_k = 60
        rerank_enabled = False
        channel_timeout_seconds = 0.05

    monkeypatch.setattr(hybrid, "get_settings", lambda: FakeSettings())

    def slow(*_args, **_kwargs):
        time.sleep(0.4)
        return [_chunk("slow#1")]

    monkeypatch.setattr(dense, "search_dense", slow)
    monkeypatch.setattr(sparse, "search_sparse", lambda q, n, f=None: [_chunk("s#1")])

    chunks, mode, debug = hybrid.hybrid_search("q")

    assert mode is RetrievalMode.SPARSE_ONLY
    assert debug["dense_error"] == "timeout"
    assert [c.chunk_id for c in chunks] == ["s#1"]


# ---------------- 重排 ----------------


def test_hybrid_skips_rerank_when_not_implemented(monkeypatch):
    monkeypatch.setattr(dense, "search_dense", lambda q, n, f=None: [_chunk("a#1")])
    monkeypatch.setattr(sparse, "search_sparse", lambda q, n, f=None: [])

    def not_implemented(*_args, **_kwargs):
        raise NotImplementedError

    monkeypatch.setattr(hybrid.rerank, "rerank", not_implemented)

    chunks, _, debug = hybrid.hybrid_search("q", rerank_enabled=True)

    assert debug["reranked"] is False
    assert [c.chunk_id for c in chunks] == ["a#1"]


def test_hybrid_applies_rerank_when_available(monkeypatch):
    monkeypatch.setattr(dense, "search_dense", lambda q, n, f=None: [_chunk("a#1"), _chunk("b#1")])
    monkeypatch.setattr(sparse, "search_sparse", lambda q, n, f=None: [])
    monkeypatch.setattr(
        hybrid.rerank, "rerank", lambda query, chunks, top_k: list(reversed(chunks))[:top_k]
    )

    chunks, _, debug = hybrid.hybrid_search("q", top_k=2, rerank_enabled=True)

    assert debug["reranked"] is True
    assert [c.chunk_id for c in chunks] == ["b#1", "a#1"]


def test_hybrid_keeps_results_when_rerank_raises(monkeypatch):
    monkeypatch.setattr(dense, "search_dense", lambda q, n, f=None: [_chunk("a#1")])
    monkeypatch.setattr(sparse, "search_sparse", lambda q, n, f=None: [])

    def boom(*_args, **_kwargs):
        raise RuntimeError("reranker overloaded")

    monkeypatch.setattr(hybrid.rerank, "rerank", boom)

    chunks, _, debug = hybrid.hybrid_search("q", rerank_enabled=True)

    assert debug["reranked"] is False
    assert [c.chunk_id for c in chunks] == ["a#1"]
