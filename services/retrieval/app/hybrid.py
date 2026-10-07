"""混合检索：BM25 + 向量 + RRF。

要点：
    * 两路并发执行，单路故障时**降级**为另一路（可用性优先），但过滤条件在两路都生效；
    * 融合用 RRF，避免 BM25 与余弦相似度的量纲不可比问题；
    * ``min_score`` 只作用于单路检索（融合分数是 RRF 值，阈值语义不同）。
"""

from __future__ import annotations

import asyncio
import time

from app.opensearch_client import BM25Retriever
from app.vector_client import VectorRetriever
from packages.common.errors import DependencyUnavailable
from packages.common.logging import get_logger
from packages.contracts import RetrieveMode, SearchHit
from packages.retrievers import FilterDict, reciprocal_rank_fusion
from packages.retrievers.base import RETRIEVER_BM25, RETRIEVER_VECTOR

logger = get_logger("retrieval.hybrid")


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


class HybridRetriever:
    def __init__(
        self,
        vector: VectorRetriever,
        bm25: BM25Retriever,
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
    ) -> tuple[str, list[SearchHit], list[SearchHit] | None]:
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

        vector_hits = self._cut(vector_result, self._min_score) if not isinstance(vector_result, BaseException) else []
        bm25_hits = self._cut(bm25_result, self._min_score) if not isinstance(bm25_result, BaseException) else []
        # ⚠️ 耗时通过实例属性回传，而 HybridRetriever 是进程内单例：
        # 并发 hybrid 请求会互相覆盖这个值，调用方拿到的可能是别的请求的耗时
        # （属性也没在 __init__ 里初始化，靠下游 getattr 默认值兜底）。
        # 它只用于 /eval 的延迟统计，不影响检索结果，所以维持 best-effort；
        # 若要把延迟做成可信指标，必须把 elapsed 随返回值一起传出去。
        self._last_elapsed = elapsed  # type: ignore[attr-defined]
        return ("hybrid", vector_hits, bm25_hits)

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

        _, vector_hits, bm25_hits = await self._both(query, vector_top_k, bm25_top_k, filters)
        timings["retrieve"] = getattr(self, "_last_elapsed", 0.0)

        started = time.perf_counter()
        fused = reciprocal_rank_fusion(
            [(RETRIEVER_VECTOR, vector_hits), (RETRIEVER_BM25, bm25_hits)],
            k=self._rrf_k,
            weights=self._weights,
            top_k=top_k,
        )
        timings["fusion"] = _ms(started)
        logger.debug("混合检索: vector=%s bm25=%s fused=%s", len(vector_hits), len(bm25_hits), len(fused))
        return fused, timings
