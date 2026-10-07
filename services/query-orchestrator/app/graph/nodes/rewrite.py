"""查询改写节点。

默认关闭（``REWRITE_ENABLED=false``）：改写会多一次模型调用，
在还没建立评测集之前，无法判断它带来的是召回提升还是语义漂移，
因此先做成**显式可回退的透传**。
"""

from __future__ import annotations

import time

from app.clients.model_gateway import ModelGatewayClient
from app.config import Settings
from app.graph.nodes.base import add_error, merge_timing
from app.graph.state import RAGState
from packages.common.logging import get_logger
from packages.contracts import ChatMessage, CompletionRequest
from packages.prompts import get_prompt_registry

logger = get_logger("orchestrator.node.rewrite")

_MAX_REWRITE_LEN = 200


def _clean(text: str) -> str:
    line = text.strip().splitlines()[0].strip() if text.strip() else ""
    return line.strip("\"“”「」'")


def make_rewrite_node(model_gateway: ModelGatewayClient, settings: Settings):
    async def rewrite(state: RAGState) -> dict:
        started = time.perf_counter()
        query = state.get("query", "")

        if not settings.rewrite_enabled:
            return merge_timing(state, "rewrite", started, rewritten_query=query)

        try:
            prompt = get_prompt_registry().render("rewrite", "v1", query=query)
            resp = await model_gateway.complete(
                CompletionRequest(
                    messages=[ChatMessage(role="user", content=prompt)],
                    temperature=0.0,
                    max_tokens=128,
                    tenant_id=state.get("tenant_id"),
                )
            )
            candidate = _clean(resp.answer)
            if candidate and len(candidate) <= _MAX_REWRITE_LEN:
                logger.debug("查询改写: %r -> %r", query, candidate)
                return merge_timing(state, "rewrite", started, rewritten_query=candidate)
            logger.warning("改写结果不可用（空或过长），沿用原查询: %r", resp.answer[:80])
            return merge_timing(
                state,
                "rewrite",
                started,
                rewritten_query=query,
                errors=add_error(state, "rewrite_rejected"),
            )
        except Exception as exc:  # noqa: BLE001 - 改写是增益项，失败必须回退而不是报错
            logger.warning("查询改写失败，沿用原查询: %s", exc)
            return merge_timing(
                state,
                "rewrite",
                started,
                rewritten_query=query,
                errors=add_error(state, f"rewrite_failed: {exc}"),
            )

    return rewrite
