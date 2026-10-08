"""混合检索：BM25 + 向量 + RRF。

要点：
    * 两路并发执行，单路故障时**降级**为另一路（可用性优先），但过滤条件在两路都生效；
    * 融合用 RRF，避免 BM25 与余弦相似度的量纲不可比问题；
    * ``min_score`` 只作用于单路检索（融合分数是 RRF 值，阈值语义不同）。
"""

from __future__ import annotations

import asyncio
import time

from packages.common.errors import DependencyUnavailable
from packages.common.logging import get_logger
from packages.contracts import RetrieveMode, SearchHit
from packages.retrievers import FilterDict, Retriever, reciprocal_rank_fusion
from packages.retrievers.base import RETRIEVER_BM25, RETRIEVER_VECTOR

logger = get_logger("retrieval.hybrid")


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


class HybridRetriever:
    """BM25 + 向量混合检索器：两路并发、单路故障降级、RRF 融合（量纲无关）。

    过滤条件在两路都生效（保证降级不改变权限边界）；``min_score`` 只在单路生效，
    因为融合后的 RRF 分数阈值语义不同。
    """

    def __init__(
        self,
        vector: Retriever,
        bm25: Retriever,
        *,
        rrf_k: int = 60,
        weight_vector: float = 1.0,
        weight_bm25: float = 1.0,
        min_score: float = 0.0,
    ) -> None:
        self._vector = vector
        self._bm25 = bm25
        self._rrf_k = rrf_k
        self._weights = {RETRIEVER_VECTOR: weight_vector, RETRIEVER_BM25: weight_bm25}
        self._min_score = min_score

    @staticmethod
    def _cut(hits: list[SearchHit], min_score: float) -> list[SearchHit]:
        if min_score <= 0:
            return hits
        return [h for h in hits if h.score >= min_score]

    async def _both(
        self, query: str, vector_top_k: int, bm25_top_k: int, filters: FilterDict | None
    ) -> tuple[str, list[SearchHit], list[SearchHit] | None, float]:
        started = time.perf_counter()
        results = await asyncio.gather(
            self._vector.retrieve(query, vector_top_k, filters),
            self._bm25.retrieve(query, bm25_top_k, filters),
            return_exceptions=True,
        )
        elapsed = _ms(started)

        vector_result, bm25_result = results[0], results[1]
        errors = [r for r in results if isinstance(r, BaseException)]
        for err in errors:
            logger.error("混合检索某一路失败，尝试降级: %s", err)

        if len(errors) == 2:
            first = errors[0]
            if isinstance(first, BaseException):
                raise first
            raise DependencyUnavailable("retrieval", "两路检索均失败")

        vector_hits = (
            self._cut(vector_result, self._min_score)
            if not isinstance(vector_result, BaseException)
            else []
        )
        bm25_hits = (
            self._cut(bm25_result, self._min_score)
            if not isinstance(bm25_result, BaseException)
            else []
        )
        # elapsed 随返回值传递。曾用实例属性回传——HybridRetriever 是单例，
        # 并发请求会互相覆盖，调用方拿到别的请求的耗时（观测数据不可信）。
        return ("hybrid", vector_hits, bm25_hits, elapsed)

    async def aclose(self) -> None:
        """关闭两路检索器的连接（向量 = Milvus gRPC，BM25 = OpenSearch）。"""
        await self._vector.aclose()
        await self._bm25.aclose()

    async def search(
        self,
        query: str,
        mode: RetrieveMode,
        *,
        top_k: int,
        vector_top_k: int,
        bm25_top_k: int,
        filters: FilterDict | None = None,
    ) -> tuple[list[SearchHit], dict[str, float]]:
        timings: dict[str, float] = {}

        if mode is RetrieveMode.vector:
            started = time.perf_counter()
            hits = await self._vector.retrieve(query, max(top_k, vector_top_k), filters)
            timings["vector"] = _ms(started)
            return self._cut(hits, self._min_score)[:top_k], timings

        if mode is RetrieveMode.keyword:
            started = time.perf_counter()
            hits = await self._bm25.retrieve(query, max(top_k, bm25_top_k), filters)
            timings["bm25"] = _ms(started)
            return self._cut(hits, self._min_score)[:top_k], timings

        _, vector_hits, bm25_hits, elapsed = await self._both(
            query, vector_top_k, bm25_top_k, filters
        )
        # 两个检索器都契约化返回 list，但降级路径可能给出 None —— 统一兜成空列表，
        # 否则下面的 len() / RRF 会直接 TypeError。
        vector_hits = vector_hits or []
        bm25_hits = bm25_hits or []
        timings["retrieve"] = elapsed

        started = time.perf_counter()
        fused = reciprocal_rank_fusion(
            [(RETRIEVER_VECTOR, vector_hits), (RETRIEVER_BM25, bm25_hits)],
            k=self._rrf_k,
            weights=self._weights,
            top_k=top_k,
        )
        timings["fusion"] = _ms(started)
        logger.debug(
            "混合检索: vector=%s bm25=%s fused=%s", len(vector_hits), len(bm25_hits), len(fused)
        )
        return fused, timings
