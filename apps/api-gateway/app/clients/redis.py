"""Redis 计数（限流用）。连接失败一律返回 None，由调用方决定策略。"""

from __future__ import annotations

from packages.common.logging import get_logger

logger = get_logger("gateway.redis")


class RedisCounter:
    """固定窗口限流计数器。

    所有方法在 Redis 不可用时返回 ``None`` 而非抛错，调用方（RateLimitMiddleware）
    据此走 fail-open 放行——限流是可用性增强项，绝不能因 Redis 抖动把正常流量
    全挡在门外。
    """

    def __init__(self, url: str) -> None:
        self._url = url
        self._redis = None

    def _client(self):
        if self._redis is None:
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(self._url, decode_responses=True)
        return self._redis

    async def incr(self, key: str, ttl_seconds: int) -> int | None:
        """自增并返回当前值；Redis 不可用时返回 None。"""
        try:
            client = self._client()
            pipe = client.pipeline()
            pipe.incr(key)
            pipe.expire(key, ttl_seconds)
            result = await pipe.execute()
            return int(result[0])
        except Exception as exc:  # noqa: BLE001
            logger.warning("Redis 计数失败（降级为放行）: %s", exc)
            return None

    async def get(self, key: str) -> int | None:
        try:
            value = await self._client().get(key)
            return int(value) if value is not None else 0
        except Exception:  # noqa: BLE001
            return None

    async def aclose(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None
