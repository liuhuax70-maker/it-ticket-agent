"""RRF 融合的单元测试（骨架阶段唯一已实现的算法）。"""

from app.retrieval.fuse import reciprocal_rank_fusion
from app.schemas.retrieval import Chunk, DocSource


def _chunk(cid: str) -> Chunk:
    return Chunk(chunk_id=cid, doc_id=cid.split("#")[0], content="x", source=DocSource.FAQ)


def test_rrf_rewards_high_rank_in_both_lists():
    """两路都排名靠前的片段，融合后应排第一。"""
    dense = [_chunk("a#1"), _chunk("b#1"), _chunk("c#1")]
    sparse = [_chunk("a#1"), _chunk("c#1"), _chunk("d#1")]

    fused = reciprocal_rank_fusion([dense, sparse])

    assert fused[0].chunk_id == "a#1"  # 两路均第 1
    assert fused[1].chunk_id == "c#1"  # 两路均第 3 / 第 2
    assert {c.chunk_id for c in fused} == {"a#1", "b#1", "c#1", "d#1"}


def test_rrf_scores_match_formula():
    """融合分数应符合 Σ 1/(k+rank)。"""
    dense = [_chunk("a#1"), _chunk("b#1")]
    sparse = [_chunk("b#1")]

    fused = reciprocal_rank_fusion([dense, sparse], k=60)
    score = {c.chunk_id: c.rrf_score for c in fused}

    assert score["a#1"] == 1 / (60 + 1)
    assert score["b#1"] == 1 / (60 + 2) + 1 / (60 + 1)


def test_rrf_assigns_rank_and_honours_top_n():
    dense = [_chunk(f"x#{i}") for i in range(1, 6)]
    fused = reciprocal_rank_fusion([dense], top_n=3)

    assert len(fused) == 3
    assert [c.rank for c in fused] == [1, 2, 3]


def test_rrf_handles_empty_input():
    assert reciprocal_rank_fusion([]) == []
    assert reciprocal_rank_fusion([[]]) == []
