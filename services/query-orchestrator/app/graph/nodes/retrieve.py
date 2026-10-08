"""检索节点：调用 retrieval 服务。

召回条数用 ``candidate_k``（大于 top_k），把「召回」和「收敛」分开：
先多召回，再由重排/截断收敛到 top_k，避免只召回 top_k 时重排无米可炊。

支持多子查询（规划节点产出）：每个子查询独立检索到 candidate_k，
再用 RRF 按名次融合。多跳问题的每类信息点都能以自己的关键词参赛，
而不是挤在一条查询里互相压制（RRF 只用排名，天然免标定）。
"""

from __future__ import annotations

import asyncio
import time

from app.clients.retrieval import RetrievalClient
from app.config import Settings
from app.graph.nodes.base import add_error, merge_timing
from app.graph.state import RAGState
from packages.common.errors import UpstreamError
from packages.common.logging import get_logger
from packages.contracts import SearchHit, SearchRequest
from packages.retrievers import reciprocal_rank_fusion

logger = get_logger("orchestrator.node.retrieve")


def _merge_by_quota(result_lists: list[list[SearchHit]], candidate_k: int) -> list[SearchHit]:
    """多子查询合并：**轮转交织 + RRF 补满**。

    两版迭代的教训都写在这里：

    1. RRF 全量融合（第一版）：RRF 奖励"在多个列表里都出现"的分块——
       多面向问题里，那是两边都沾一点边的平庸分块；每个面向真正的最佳分块
       只在自己的列表里出现，融合分反而低，被挤出 top-k
       （实测：multi-offboard 被误拒、multi-train-expense 的时限分块缺席）。
    2. 按子查询分段配额（第二版）：候选池里每个面向都有了，但排列是
       "sub0 的前 10 条，然后 sub1 的前 10 条"——下游截断到 top_k 时
       **全部被第一个面向占据**，等于白做（截断后仍全是训练制度分块）。

    所以最终形态是**轮转交织**：各列表的第 1 名先依次入选，再第 2 名……
    这样无论下游在哪里截断，前缀里每个面向都有代表；尾部用 RRF 补满
    （被多路共同召回的优先），兼顾排名融合。
    """
    selected: list[SearchHit] = []
    seen: set[str] = set()
    depth = 0
    max_len = max((len(hits) for hits in result_lists), default=0)
    while len(selected) < candidate_k and depth < max_len:
        for hits in result_lists:
            if depth >= len(hits):
                continue
            hit = hits[depth]
            if hit.chunk_id in seen:
                continue
            seen.add(hit.chunk_id)
            selected.append(hit)
            if len(selected) >= candidate_k:
                break
        depth += 1

    fused = reciprocal_rank_fusion(
        [(f"sub{i}", hits) for i, hits in enumerate(result_lists)],
        top_k=candidate_k,
    )
    for hit in fused:
        if len(selected) >= candidate_k:
            break
        if hit.chunk_id not in seen:
            seen.add(hit.chunk_id)
            selected.append(hit)
    return selected[:candidate_k]


def make_retrieve_node(retrieval: RetrievalClient, settings: Settings):
    async def retrieve(state: RAGState) -> dict:
        started = time.perf_counter()
        query = state.get("rewritten_query") or state.get("query", "")
        top_k = state.get("top_k") or settings.top_k
        candidate_k = max(top_k, settings.candidate_k)
        # 规划节点总会产出 sub_queries（透传时是 [原查询]）；这里做兜底以防节点被摘
        sub_queries = [q for q in (state.get("sub_queries") or [query]) if q] or [query]
        mode = state.get("mode") or settings.default_mode()

        async def _search_one(sub_query: str) -> list[SearchHit]:
            req = SearchRequest(
                query=sub_query,
                top_k=candidate_k,
                mode=mode,
                acl=state.get("acl"),
            )
            resp = await retrieval.search(req)
            return list(resp.hits)

        try:
            if len(sub_queries) == 1:
                hits = await _search_one(sub_queries[0])
            else:
                # 子查询之间相互独立，并发检索；ACL 是同一份，无越权面变化
                result_lists = await asyncio.gather(*[_search_one(q) for q in sub_queries])
                hits = _merge_by_quota(result_lists, candidate_k)
        except UpstreamError as exc:
            # 检索服务不可用属于依赖故障，必须向上抛（不能被当成"没检索到"而静默拒答）
            logger.error("检索失败: %s", exc)
            raise

        logger.debug(
            "召回 %s 条（sub_queries=%s candidate_k=%s mode=%s）",
            len(hits),
            len(sub_queries),
            candidate_k,
            mode.value,
        )
        timings = dict(state.get("timings") or {})
        timings["retrieve"] = round((time.perf_counter() - started) * 1000, 1)
        if len(sub_queries) > 1:
            timings["retrieve.sub_queries"] = float(len(sub_queries))
        return {"hits": hits, "timings": timings, "errors": list(state.get("errors") or [])}

    return retrieve


__all__ = ["make_retrieve_node", "add_error", "merge_timing"]
