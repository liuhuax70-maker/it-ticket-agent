"""租户配额与成本统计。

默认 ``QUOTA_ENABLED=false``：只统计、不拦截，避免最小闭环被配额误伤。
开启后按「租户 + UTC 日期」计数，超限抛 429。
"""

from __future__ import annotations

import datetime as dt

from packages.common.errors import RagError
from packages.common.logging import get_logger

logger = get_logger("model_gateway.quota")


class QuotaExceeded(RagError):
    status_code = 429


class QuotaGuard:
    def __init__(self, redis_url: str, daily_limit: int, enabled: bool) -> None:
        self._redis_url = redis_url
        self._daily_limit = daily_limit
        self.enabled = enabled
        self._redis = None

    def _client(self):
        if self._redis is None:
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(self._redis_url, decode_responses=True)
        return self._redis

    @staticmethod
    def _key(tenant_id: str) -> str:
        today = dt.datetime.now(dt.UTC).strftime("%Y%m%d")
        return f"quota:tokens:{tenant_id}:{today}"

    async def consume(self, tenant_id: str | None, tokens: int) -> int:
        """累加用量并返回当日累计值。Redis 故障不影响主流程。"""
        if not tenant_id or tokens <= 0:
            return 0
        try:
            client = self._client()
            key = self._key(tenant_id)
            used = int(await client.incrby(key, tokens))
            if used == tokens:  # 首次写入设置过期，避免 key 无限累积
                await client.expire(key, 60 * 60 * 48)
            if self.enabled and used > self._daily_limit:
                raise QuotaExceeded(
                    f"租户 {tenant_id} 当日 token 配额已用尽（{used}/{self._daily_limit}）"
                )
            return used
        except QuotaExceeded:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("配额统计失败（忽略）: %s", exc)
            return 0

    async def snapshot(self, tenant_id: str) -> dict[str, int | bool]:
        try:
            used = int(await self._client().get(self._key(tenant_id)) or 0)
        except Exception:  # noqa: BLE001
            used = 0
        return {"used_today": used, "daily_limit": self._daily_limit, "enforced": self.enabled}

    async def aclose(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None
