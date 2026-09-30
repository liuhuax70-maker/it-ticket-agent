"""相关性门槛单元测试（纯函数，不依赖 Milvus 与模型）。"""

from app.retrieval.hybrid import passes_relevance_gate
from app.schemas.retrieval import Chunk, DocSource


def _chunk(chunk_id: str, dense: float | None) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        doc_id=chunk_id.split("#")[0],
        content="内容",
        source=DocSource.FAQ,
        dense_score=dense,
    )


def test_disabled_when_threshold_not_positive():
    """门槛 <= 0 表示不启用，必须恒定通过（否则会误伤不想启用门槛的场景）。"""
    chunks = [_chunk("a#0", 0.1)]
    assert passes_relevance_gate(chunks, 0) is True
    assert passes_relevance_gate(chunks, -1.0) is True


def test_passes_when_any_chunk_reaches_threshold():
    """只要有一条片段足够相关，就认为有可用资料。"""
    chunks = [_chunk("a#0", 0.30), _chunk("b#0", 0.45)]
    assert passes_relevance_gate(chunks, 0.42) is True


def test_rejects_when_all_below_threshold():
    chunks = [_chunk("a#0", 0.30), _chunk("b#0", 0.41)]
    assert passes_relevance_gate(chunks, 0.42) is False


def test_exact_boundary_passes():
    """边界值应判定为通过（>=）。"""
    assert passes_relevance_gate([_chunk("a#0", 0.42)], 0.42) is True
    assert passes_relevance_gate([_chunk("a#0", 0.419)], 0.42) is False


def test_all_scores_missing_passes_instead_of_rejecting():
    """稠密通道降级为纯 BM25 时所有片段都没有 dense_score。

    此时**无法判定**相关性，必须放行 —— 否则纯 BM25 模式下每个问题都会被判成
    「无资料」，整条链路直接瘫痪（这是实现时踩到的真实 bug）。
    """
    assert passes_relevance_gate([_chunk("a#0", None), _chunk("b#0", None)], 0.42) is True


def test_missing_score_does_not_block_other_chunks():
    """部分片段缺分（只被稀疏通道命中）时，仍以有分的片段为准判定。"""
    chunks = [_chunk("a#0", None), _chunk("b#0", 0.45)]
    assert passes_relevance_gate(chunks, 0.42) is True

    chunks = [_chunk("a#0", None), _chunk("b#0", 0.30)]
    assert passes_relevance_gate(chunks, 0.42) is False


def test_empty_chunks_passes():
    """空结果属于「无命中」，不是「被门槛拦截」，两者在调用方已区分开；
    这里同样按「无法判定」放行，避免把无命中二次标记为门槛拦截。
    """
    assert passes_relevance_gate([], 0.42) is True
