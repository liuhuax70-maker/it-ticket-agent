"""查询缓存节点：命中即短路，未命中则在链路末端回填。"""

from __future__ import annotations

import time

from app.clients.cache import QueryCache
from app.config import Settings
from app.graph.nodes.base import merge_timing, ms
from app.graph.state import RAGState
from packages.common.logging import get_logger
from packages.contracts import Citation
from packages.observability.metrics import CACHE_LOOKUP_COUNTER

logger = get_logger("orchestrator.node.cache")


def _cache_key(
    state: RAGState, settings: Settings
) -> tuple[str, str, str, str, int, str, float | None, str | None]:
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
        # 请求指定的模型：换了模型答案必然不同，必须分键，否则会命中别的模型的旧答案
        state.get("model"),
    )


def make_cache_lookup_node(cache: QueryCache, settings: Settings):
    """构造缓存查询节点：命中即短路返回缓存答案；跳过/未命中/命中分别计入 ``CACHE_LOOKUP_COUNTER``。

    跳过（缓存未开或 ``use_cache=False``）与未命中必须分开计数——把"没开"算成未命中
    会让命中率看起来永远很低。
    """
    async def cache_lookup(state: RAGState) -> dict:
        started = time.perf_counter()
        if not cache.enabled or not state.get("use_cache", True):
            # 两种"跳过"都记 skip：不给指标加新标签维度（那会让历史序列不可比，
            # 而"为什么跳过"并不是运维需要区分的事）。
            # skip 与 miss 必须分开：把"缓存没开"算成未命中会让命中率看起来永远很低。
            CACHE_LOOKUP_COUNTER.inc({"result": "skip"})
            return merge_timing(state, "cache_lookup", started, cached=False)

        tenant_id, department_id, user_id, mode, top_k, query, temperature, model = _cache_key(
            state, settings
        )
        payload = await cache.get(
            tenant_id,
            department_id,
            user_id,
            mode,
            top_k,
            query,
            temperature,
            settings.cache_version,
            model,
        )
        if payload is None:
            CACHE_LOOKUP_COUNTER.inc({"result": "miss"})
            return merge_timing(state, "cache_lookup", started, cached=False)

        CACHE_LOOKUP_COUNTER.inc({"result": "hit"})
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
    """构造缓存写入节点：链路末端回填缓存。

    拒答与 ``use_cache=False`` 不写：拒答固化会污染后续正确回答，评测流量写缓存
    会让下一轮评测拿到命中、改变检索侧指标的样本分母。
    """
    async def cache_store(state: RAGState) -> dict:
        started = time.perf_counter()
        # use_cache=False 的请求**既不读也不写**：让评测流量灌进生产缓存，
        # 会让下一轮评测拿到一堆命中，检索侧指标的样本分母随之变化。
        if not cache.enabled or not state.get("use_cache", True) or state.get("cached"):
            return {}
        # 不缓存拒答：把「资料缺失」固化下来，会在文档补录后继续吐旧答案
        if state.get("refused"):
            return merge_timing(state, "cache_store", started)
        tenant_id, department_id, user_id, mode, top_k, query, temperature, model = _cache_key(
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
            settings.cache_version,
            model,
        )
        return merge_timing(state, "cache_store", started)

    return cache_store


__all__ = ["make_cache_lookup_node", "make_cache_store_node", "ms"]
