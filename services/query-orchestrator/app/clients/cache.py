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
    def _key(
        tenant_id: str,
        department_id: str,
        user_id: str,
        mode: str,
        top_k: int,
        query: str,
        temperature: float | None = None,
    ) -> str:
        """组装缓存键。

        这里每个分量都不是"可选的"，少一个就会出事：

        ``tenant_id / department_id / user_id``
            **ACL 过滤依赖这三个维度。** 早期实现只用
            ``(tenant_id, mode, top_k, query)``，结果同租户内 alice（hr）与
            bob（engineering）问同一句问题时共用同一条缓存——bob 会直接收到
            alice 那条**带 HR 文档引用**的答案，检索层的 ACL 过滤被整段绕过。
            这类漏洞在缓存关闭时完全不可见，只在生产开启缓存后才暴露。
            private 可见性按 owner 过滤，所以 ``user_id`` 不能省。
        ``mode / top_k``
            检索方式与召回条数不同，答案就不同。
        ``temperature``
            生成温度不同答案就不同；评测会把上一轮的结果当成本轮结果。
        ``query``
            归一化后参与摘要，避免空白差异导致无谓穿透。
        """
        parts = "|".join(
            [
                tenant_id or "-",
                department_id or "-",
                user_id or "-",
                mode,
                str(top_k),
                f"{temperature}",
                normalize(query),
            ]
        )
        digest = hashlib.sha256(parts.encode()).hexdigest()
        return f"rag:cache:{digest}"

    async def get(
        self,
        tenant_id: str,
        department_id: str,
        user_id: str,
        mode: str,
        top_k: int,
        query: str,
        temperature: float | None = None,
    ) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        try:
            raw = await self._client().get(
                self._key(tenant_id, department_id, user_id, mode, top_k, query, temperature)
            )
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
        self,
        tenant_id: str,
        department_id: str,
        user_id: str,
        mode: str,
        top_k: int,
        query: str,
        payload: dict[str, Any],
        temperature: float | None = None,
    ) -> None:
        if not self.enabled:
            return
        try:
            await self._client().set(
                self._key(tenant_id, department_id, user_id, mode, top_k, query, temperature),
                json.dumps(payload, ensure_ascii=False),
                ex=self._ttl,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("缓存写入失败（忽略）: %s", exc)

    async def aclose(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None
