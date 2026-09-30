"""RRF（Reciprocal Rank Fusion）融合。

按**排名**而非分数融合，避免稠密/稀疏两路分数量纲不一致：

    RRF_score(d) = Σ_i 1 / (k + rank_i(d))

其中 rank 从 1 开始，k 为常数（默认 60）。
"""

from collections import defaultdict

from app.schemas.retrieval import Chunk

#: RRF 常数 k，取值与 `开发流程/04-检索与编排设计.md` §2.6 一致
DEFAULT_RRF_K = 60


def reciprocal_rank_fusion(
    ranked_lists: list[list[Chunk]],
    k: int = DEFAULT_RRF_K,
    top_n: int | None = None,
) -> list[Chunk]:
    """融合多路有序检索结果。

    :param ranked_lists: 各路结果，**必须按相关性从高到低排列**（rank 由位置决定）。
    :param k: RRF 常数。
    :param top_n: 只返回前 N 条（None 表示全部）。
    :return: 融合后的 Chunk 列表，已写入 rrf_score 与 rank。
    """
    scores: dict[str, float] = defaultdict(float)
    by_id: dict[str, Chunk] = {}

    for ranked in ranked_lists:
        for rank, chunk in enumerate(ranked, start=1):
            scores[chunk.chunk_id] += 1.0 / (k + rank)
            by_id.setdefault(chunk.chunk_id, chunk)

    ordered = sorted(by_id.values(), key=lambda c: scores[c.chunk_id], reverse=True)

    result: list[Chunk] = []
    for idx, chunk in enumerate(ordered, start=1):
        result.append(chunk.model_copy(update={"rrf_score": scores[chunk.chunk_id], "rank": idx}))
        if top_n is not None and len(result) >= top_n:
            break
    return result
