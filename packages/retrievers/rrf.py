"""RRF（Reciprocal Rank Fusion）多路结果融合。

为什么用 RRF 而不是分数加权：BM25 与余弦相似度的量纲不可比，
按分数加权需要逐数据集调参；RRF 只用**排名**，天然免标定。
"""

from __future__ import annotations

from packages.contracts import SearchHit

DEFAULT_RRF_K = 60


def reciprocal_rank_fusion(
    result_lists: list[tuple[str, list[SearchHit]]],
    *,
    k: int = DEFAULT_RRF_K,
    weights: dict[str, float] | None = None,
    top_k: int | None = None,
) -> list[SearchHit]:
    """融合多路检索结果。

    Args:
        result_lists: ``[(retriever_name, hits), ...]``，顺序即排名（已按各自分数降序）。
        k: RRF 平滑常数，经验值 60。
        weights: 各路权重，缺省 1.0。
        top_k: 截断长度，None 表示不截断。
    """
    weights = weights or {}
    scores: dict[str, float] = {}
    best: dict[str, SearchHit] = {}
    sources: dict[str, list[str]] = {}

    for name, hits in result_lists:
        weight = weights.get(name, 1.0)
        for rank, hit in enumerate(hits, start=1):
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + weight / (k + rank)
            sources.setdefault(hit.chunk_id, []).append(name)
            # 保留信息量最全的那条（字段非空的优先）
            current = best.get(hit.chunk_id)
            if current is None or (not current.text and hit.text):
                best[hit.chunk_id] = hit

    fused: list[SearchHit] = []
    for chunk_id, score in sorted(scores.items(), key=lambda kv: kv[1], reverse=True):
        hit = best[chunk_id].model_copy(deep=True)
        hit.score = round(score, 6)
        hit.retriever = "+".join(sources.get(chunk_id, []))
        fused.append(hit)

    return fused[:top_k] if top_k else fused
