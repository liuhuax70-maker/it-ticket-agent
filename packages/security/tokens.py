"""Keycloak 令牌获取（密码模式），供**内部工具与评测**使用。

定位说明（避免被误用）：
    这里是资源所有者密码模式，服务端需要知道用户口令。它适合**评测、压测、
    本地联调**这类"我们本来就有测试账号"的场景；真实用户登录必须走浏览器侧的
    授权码 + PKCE（见 apps/api-gateway 前端），服务端永远不该碰用户口令。

设计取舍：
    换不到令牌时返回 ``None`` 而不是抛异常——鉴权关闭（AUTHZ_ENABLED=false）是
    合法配置，此时调用方应回退到固定身份继续跑，而不是整个评测失败。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from packages.common.logging import get_logger
from packages.security.config import SecuritySettings

logger = get_logger("security.tokens")


@dataclass
class _Cached:
    access_token: str
    expires_at: float


@dataclass
class TokenProvider:
    """按用户名换取并缓存访问令牌。

    默认约定「口令 == 用户名」（开发环境测试账号），可显式传入 ``password`` 覆盖。
    """

    settings: SecuritySettings
    passwords: dict[str, str] = field(default_factory=dict)
    # 提前量：令牌剩余时间不足这么多秒就重新换，避免请求发出时刚好过期
    refresh_slack: float = 30.0
    _cache: dict[str, _Cached] = field(default_factory=dict, init=False)
    _client: httpx.AsyncClient | None = field(default=None, init=False)
    _unavailable: bool = field(default=False, init=False)

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=20.0)
        return self._client

    def cached(self, username: str) -> str | None:
        item = self._cache.get(username)
        if item and item.expires_at - self.refresh_slack > time.time():
            return item.access_token
        return None

    async def token(self, username: str, password: str | None = None) -> str | None:
        if self._unavailable:
            return None
        cached = self.cached(username)
        if cached:
            return cached

        payload: dict[str, Any] = {
            "grant_type": "password",
            "client_id": self.settings.keycloak_client_id,
            "username": username,
            "password": password or self.passwords.get(username, username),
            "scope": "openid",
        }
        if self.settings.keycloak_client_secret:
            payload["client_secret"] = self.settings.keycloak_client_secret

        try:
            resp = await self._http().post(self.settings.token_url(), data=payload)
            resp.raise_for_status()
            body = resp.json()
        except Exception as exc:  # noqa: BLE001
            # 只在第一次失败时告警，避免逐条样本刷屏
            if not self._unavailable:
                self._unavailable = True
                logger.warning(
                    "无法从 Keycloak 换取令牌（%s）：%s；将回退为固定身份继续执行",
                    self.settings.token_url(),
                    exc,
                )
            return None

        expires_in = float(body.get("expires_in", 300))
        self._cache[username] = _Cached(
            access_token=str(body["access_token"]), expires_at=time.time() + expires_in
        )
        logger.debug("已获取 %s 的令牌（%ss 有效）", username, expires_in)
        return str(body["access_token"])

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


__all__ = ["TokenProvider"]
