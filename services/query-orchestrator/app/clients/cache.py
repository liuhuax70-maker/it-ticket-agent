"""Redis 查询缓存。

当前实现是**精确匹配**：把 ``(tenant_id, department_id, user_id, mode, top_k,
temperature, normalize(query))`` 七个分量拼起来取哈希。
分量清单与每个分量的理由见 :meth:`QueryCache._key` 的文档字符串——
**改动键的构成前先读那段注释**，identity 相关的三个分量少一个就是越权
（同租户内 alice 会把带 HR 引用的答案共享给 bob，见 docs/adr/0006）。

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
    """归一化查询：折叠空白、转小写，减少无谓缓存穿透。"""
    return _WS.sub(" ", query).strip().lower()


class QueryCache:
    """Redis 查询缓存（**精确匹配**）。

    键由 7 个身份/请求分量哈希构成，少一个分量即越权（同租户内串答案，见模块 docstring
    与 ``_key``）。故障一律降级为未命中/不写，不阻断主链路；``version`` 用于换模型/提示词后
    显式失效。
    """

    def __init__(self, redis_url: str, ttl_seconds: int = 3600, enabled: bool = False) -> None:
        self.enabled = enabled
        self._ttl = ttl_seconds
        self._url = redis_url
        # 延迟建连：构造时不连Redis（服务启动不应依赖 Redis 可用）
        self._redis: Any = None

    def _client(self) -> Any:
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
        version: str = "1",
        model: str | None = None,
    ) -> str:
        """组装缓存键。

        ``tenant_id/department_id/user_id`` 缺一即失效：ACL 过滤依赖这三个维度，少了任一个
        都会让同租户内不同部门共用同一条答案，检索层 ACL 被整段绕过，且缓存关闭时完全不可见。
        private 按 owner 过滤，所以 ``user_id`` 不能省。

        ``model`` 是调用方逐请求指定的生成模型：模型不同则答案必然不同，必须分键。
        它是**条件追加**的分量——留空表示"用网关默认模型"，此时键与引入该参数前**逐字节相同**，
        升级后默认路径仍命中存量缓存；只有显式选了模型才会分出新键。

        ``version``（``CACHE_VERSION``）仍是显式失效开关，应对缓存键表达不了的**全局**变化——
        改提示词、改切分参数这类影响所有请求、且编排层无从得知具体取值的改动。
        （过去"换作答模型"也归它管，但模型现在逐请求可选，已由 ``model`` 分量直接覆盖。）
        """
        parts = [
            version or "1",
            tenant_id or "-",
            department_id or "-",
            user_id or "-",
            mode,
            str(top_k),
            f"{temperature}",
            normalize(query),
        ]
        # 只在显式选模型时追加，保证 model=None 的键与旧实现完全一致
        if model:
            parts.append(model)
        digest = hashlib.sha256("|".join(parts).encode()).hexdigest()
        return f"rag:cache:v{version or '1'}:{digest}"
        digest = hashlib.sha256(parts.encode()).hexdigest()
        return f"rag:cache:v{version or '1'}:{digest}"

    async def get(
        self,
        tenant_id: str,
        department_id: str,
        user_id: str,
        mode: str,
        top_k: int,
        query: str,
        temperature: float | None = None,
        version: str = "1",
        model: str | None = None,
    ) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        try:
            raw = await self._client().get(
                self._key(
                    tenant_id, department_id, user_id, mode, top_k, query, temperature, version, model
                )
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
        version: str = "1",
        model: str | None = None,
    ) -> None:
        if not self.enabled:
            return
        try:
            await self._client().set(
                self._key(
                    tenant_id, department_id, user_id, mode, top_k, query, temperature, version, model
                ),
                json.dumps(payload, ensure_ascii=False),
                ex=self._ttl,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("缓存写入失败（忽略）: %s", exc)

    async def aclose(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None
