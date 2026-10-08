"""向量化结果缓存（Redis）。

用途是「重跑索引不重复算 embedding」，默认关闭：缓存会污染延迟观测，
必须先有干净基线（旧 P0 结论，沿用）。
"""

from __future__ import annotations

import hashlib
import json

from packages.common.logging import get_logger

logger = get_logger("embeddings.cache")


def _key(model: str, kind: str, text: str) -> str:
    digest = hashlib.sha256(f"{model}|{kind}|{text}".encode()).hexdigest()
    return f"embed:{digest}"


class EmbeddingCache:
    """向量化结果 Redis 缓存：重跑索引时复用已算向量，避免重复计算。

    默认关闭（``embed_cache_enabled=False``）：缓存会污染延迟观测、掩盖真正的性能问题，
    必须先有干净基线（旧 P0 结论）。读取/写入故障均降级为「全量计算」，不阻断主流程。
    """
    def __init__(self, redis_url: str, ttl_seconds: int = 3600) -> None:
        import redis.asyncio as aioredis

        self._redis = aioredis.from_url(redis_url, decode_responses=True)
        self._ttl = ttl_seconds

    async def get_many(self, model: str, kind: str, texts: list[str]) -> list[list[float] | None]:
        if not texts:
            return []
        keys = [_key(model, kind, t) for t in texts]
        try:
            raw = await self._redis.mget(keys)
        except Exception as exc:  # noqa: BLE001 - 缓存故障不应阻断主流程
            logger.warning("embedding 缓存读取失败，降级为全量计算: %s", exc)
            return [None] * len(texts)
        return [json.loads(v) if v else None for v in raw]

    async def set_many(
        self, model: str, kind: str, texts: list[str], vectors: list[list[float]]
    ) -> None:
        if not texts:
            return
        try:
            pipe = self._redis.pipeline()
            for text, vec in zip(texts, vectors, strict=True):
                pipe.set(_key(model, kind, text), json.dumps(vec), ex=self._ttl)
            await pipe.execute()
        except Exception as exc:  # noqa: BLE001
            logger.warning("embedding 缓存写入失败（忽略）: %s", exc)

    async def aclose(self) -> None:
        await self._redis.aclose()
