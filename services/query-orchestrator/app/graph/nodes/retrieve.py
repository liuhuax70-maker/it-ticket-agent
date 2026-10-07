"""检索节点：调用 retrieval 服务。

召回条数用 ``candidate_k``（大于 top_k），把「召回」和「收敛」分开：
先多召回，再由重排/截断收敛到 top_k，避免只召回 top_k 时重排无米可炊。
"""

from __future__ import annotations

import time

from app.clients.retrieval import RetrievalClient
from app.config import Settings
from app.graph.nodes.base import add_error, merge_timing
from app.graph.state import RAGState
from packages.common.errors import UpstreamError
from packages.common.logging import get_logger
from packages.contracts import SearchRequest

logger = get_logger("orchestrator.node.retrieve")


def make_retrieve_node(retrieval: RetrievalClient, settings: Settings):
    async def retrieve(state: RAGState) -> dict:
        started = time.perf_counter()
        query = state.get("rewritten_query") or state.get("query", "")
        top_k = state.get("top_k") or settings.top_k
        candidate_k = max(top_k, settings.candidate_k)

        req = SearchRequest(
            query=query,
            top_k=candidate_k,
            mode=state.get("mode") or settings.default_mode(),
            acl=state.get("acl"),
        )
        try:
            resp = await retrieval.search(req)
        except UpstreamError as exc:
            # 检索服务不可用属于依赖故障，必须向上抛（不能被当成"没检索到"而静默拒答）
            logger.error("检索失败: %s", exc)
            raise

        logger.debug(
            "召回 %s 条（candidate_k=%s mode=%s）", len(resp.hits), candidate_k, req.mode.value
        )
        timings = dict(state.get("timings") or {})
        timings["retrieve"] = round((time.perf_counter() - started) * 1000, 1)
        for key, value in (resp.timings_ms or {}).items():
            timings[f"retrieve.{key}"] = value
        return {"hits": resp.hits, "timings": timings, "errors": list(state.get("errors") or [])}

    return retrieve


__all__ = ["make_retrieve_node", "add_error", "merge_timing"]
