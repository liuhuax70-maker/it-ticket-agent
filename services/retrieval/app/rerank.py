"""重排。

默认关闭（``RERANK_ENABLED=false``）：没有 eval set 就调重排，好坏无法归因。
关闭时是**显式的透传**（返回融合顺序），而不是假装重排过——返回体里
``reranker`` 字段会如实写 ``rrf``。

开启后使用 fastembed 的 cross-encoder 本地打分（无需外部服务）。
"""

from __future__ import annotations

import asyncio
import time

from packages.common.logging import get_logger
from packages.contracts import SearchHit

logger = get_logger("retrieval.rerank")


class Reranker:
    """可选重排器：cross-encoder 本地打分（无需外部服务）。

    关闭时**显式透传**（``reranker="rrf"``），不假装重排过；模型加载/打分失败均降级为
    融合顺序，避免重排这一步把整个检索链路拖挂。
    """

    def __init__(self, enabled: bool, model_name: str) -> None:
        self.enabled = enabled
        self.model_name = model_name
        self._model = None
        self._load_failed = False

    def _load(self):
        if self._model is not None or self._load_failed:
            return self._model
        try:
            from fastembed.rerank.cross_encoder import TextCrossEncoder

            self._model = TextCrossEncoder(model_name=self.model_name)
            logger.info("重排模型已加载: %s", self.model_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("重排模型加载失败，降级为透传: %s", exc)
            self._load_failed = True
            self._model = None
        return self._model

    def _score_sync(self, query: str, hits: list[SearchHit]) -> list[float]:
        model = self._load()
        if model is None:
            return [h.score for h in hits]
        return [float(s) for s in model.rerank(query, [h.text for h in hits])]

    async def rerank(
        self, query: str, hits: list[SearchHit], top_k: int
    ) -> tuple[list[SearchHit], str, float]:
        started = time.perf_counter()
        if not hits:
            return [], "rrf", 0.0

        if not self.enabled:
            return hits[:top_k], "rrf", round((time.perf_counter() - started) * 1000, 1)

        try:
            scores = await asyncio.to_thread(self._score_sync, query, hits)
        except Exception as exc:  # noqa: BLE001
            logger.error("重排失败，降级为融合顺序: %s", exc)
            return hits[:top_k], "rrf", round((time.perf_counter() - started) * 1000, 1)

        model = self._load()
        if model is None:
            return hits[:top_k], "rrf", round((time.perf_counter() - started) * 1000, 1)

        scored = list(zip(hits, scores, strict=True))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        out: list[SearchHit] = []
        for hit, score in scored[:top_k]:
            updated = hit.model_copy(deep=True)
            updated.score = round(score, 6)
            updated.retriever = "rerank"
            out.append(updated)
        return (
            out,
            f"cross-encoder:{self.model_name}",
            round((time.perf_counter() - started) * 1000, 1),
        )
