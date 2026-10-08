"""重排节点 + 上下文构造。

此处是**引用编号的诞生地**：重排后的顺序即 contexts 顺序，
citations 必须由同一份 contexts 派生（见 guard 节点）。
"""

from __future__ import annotations

import time

from app.clients.retrieval import RetrievalClient
from app.config import Settings
from app.graph.nodes.base import merge_timing
from app.graph.state import RAGState
from app.prompts import build_context_items
from packages.common.logging import get_logger
from packages.contracts import RerankRequest

logger = get_logger("orchestrator.node.rerank")


def make_rerank_node(retrieval: RetrievalClient, settings: Settings):
    """构造重排节点：可选 cross-encoder 重排并构造 contexts（**引用编号诞生地**），失败回退融合顺序。"""

    async def rerank(state: RAGState) -> dict:
        started = time.perf_counter()
        hits = list(state.get("hits") or [])
        top_k = state.get("top_k") or settings.top_k
        reranker_name = "rrf"

        if settings.rerank_enabled and hits:
            try:
                resp = await retrieval.rerank(
                    RerankRequest(
                        query=state.get("rewritten_query") or state.get("query", ""),
                        hits=hits,
                        top_k=top_k,
                    )
                )
                hits = resp.hits
                reranker_name = resp.reranker
            except Exception as exc:  # noqa: BLE001 - 重排是增益项，失败回退到融合顺序
                logger.warning("重排失败，回退到融合顺序: %s", exc)

        hits = hits[:top_k]
        contexts = build_context_items(hits)
        return merge_timing(
            state, "rerank", started, hits=hits, contexts=contexts, reranker=reranker_name
        )

    return rerank
