"""Redis 查询缓存。

当前实现是**精确匹配**（租户 + 模式 + top_k + 规范化查询的哈希）：
最小闭环里先拿这份可解释、零误判的缓存做延迟基线。

为什么现在不做向量相似匹配：相似度阈值没有 eval set 就无法标定，
阈值偏松会把「入职体检报销」和「年度体检报销」当成同一个问题，
返回错误答案却看起来像缓存命中——那是比慢更严重的问题。
TODO(P4)：等 eval set 建立后，升级为「embedding 相似度 + 阈值」的两级缓存。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from packages.common.logging import get_logger

logger = get_logger("orchestrator.cache")

_WS = re.compile(r"\s+")


def normalize(query: str) -> str:
    return _WS.sub(" ", query).strip().lower()


class QueryCache:
    def __init__(self, redis_url: str, ttl_seconds: int = 3600, enabled: bool = False) -> None:
        self.enabled = enabled
        self._ttl = ttl_seconds
        self._url = redis_url
        self._redis = None

    def _client(self):
        if self._redis is None:
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(self._url, decode_responses=True)
        return self._redis

    @staticmethod
    def _key(tenant_id: str, mode: str, top_k: int, query: str) -> str:
        digest = hashlib.sha256(
            f"{tenant_id}|{mode}|{top_k}|{normalize(query)}".encode("utf-8")
        ).hexdigest()
        return f"rag:cache:{digest}"

    async def get(self, tenant_id: str, mode: str, top_k: int, query: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        try:
            raw = await self._client().get(self._key(tenant_id, mode, top_k, query))
        except Exception as exc:  # noqa: BLE001 - 缓存故障必须降级而不是报错
            logger.warning("缓存读取失败，降级为未命中: %s", exc)
            return None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("缓存内容损坏，已忽略")
            return None

    async def set(
        self, tenant_id: str, mode: str, top_k: int, query: str, payload: dict[str, Any]
    ) -> None:
        if not self.enabled:
            return
        try:
            await self._client().set(
                self._key(tenant_id, mode, top_k, query),
                json.dumps(payload, ensure_ascii=False),
                ex=self._ttl,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("缓存写入失败（忽略）: %s", exc)

    async def aclose(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None
