"""查询缓存节点：命中即短路，未命中则在链路末端回填。"""

from __future__ import annotations

import time

from app.clients.cache import QueryCache
from app.config import Settings
from app.graph.nodes.base import merge_timing, ms
from app.graph.state import RAGState
from packages.common.logging import get_logger
from packages.contracts import Citation

logger = get_logger("orchestrator.node.cache")


def _cache_key(
    state: RAGState, settings: Settings
) -> tuple[str, str, str, str, int, str, float | None]:
    """缓存键分量，见 QueryCache._key 的说明（身份维度缺一即越权风险）。"""
    mode = (state.get("mode") or settings.default_mode()).value
    top_k = state.get("top_k") or settings.top_k
    return (
        state.get("tenant_id") or settings.default_tenant_id,
        state.get("department_id") or "",
        state.get("user_id") or "",
        mode,
        top_k,
        state.get("query", ""),
        state.get("temperature"),
    )


def make_cache_lookup_node(cache: QueryCache, settings: Settings):
    async def cache_lookup(state: RAGState) -> dict:
        started = time.perf_counter()
        if not cache.enabled:
            return merge_timing(state, "cache_lookup", started, cached=False)

        tenant_id, department_id, user_id, mode, top_k, query, temperature = _cache_key(
            state, settings
        )
        payload = await cache.get(
            tenant_id, department_id, user_id, mode, top_k, query, temperature
        )
        if payload is None:
            return merge_timing(state, "cache_lookup", started, cached=False)

        logger.info("命中查询缓存 tenant=%s mode=%s", tenant_id, mode)
        return merge_timing(
            state,
            "cache_lookup",
            started,
            cached=True,
            answer=payload.get("answer", ""),
            citations=[Citation.model_validate(c) for c in payload.get("citations", [])],
            refused=bool(payload.get("refused", False)),
            model=payload.get("model"),
        )

    return cache_lookup


def make_cache_store_node(cache: QueryCache, settings: Settings):
    async def cache_store(state: RAGState) -> dict:
        started = time.perf_counter()
        if not cache.enabled or state.get("cached"):
            return {}
        # 不缓存拒答：把「资料缺失」固化下来，会在文档补录后继续吐旧答案
        if state.get("refused"):
            return merge_timing(state, "cache_store", started)
        tenant_id, department_id, user_id, mode, top_k, query, temperature = _cache_key(
            state, settings
        )
        await cache.set(
            tenant_id,
            department_id,
            user_id,
            mode,
            top_k,
            query,
            {
                "answer": state.get("answer", ""),
                "citations": [c.model_dump(mode="json") for c in (state.get("citations") or [])],
                "refused": False,
                "model": state.get("model"),
            },
            temperature,
        )
        return merge_timing(state, "cache_store", started)

    return cache_store


__all__ = ["make_cache_lookup_node", "make_cache_store_node", "ms"]
