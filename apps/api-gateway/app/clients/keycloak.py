"""Keycloak 客户端。

用户令牌的**校验**在 ``packages.security.identity``（走 JWKS，无需与 Keycloak 交互）；
本模块负责需要 Keycloak 配合的部分：服务账号令牌与可达性探测，
供 eval / 定时任务等非交互场景调用平台接口。
"""

from __future__ import annotations

import httpx

from packages.common.errors import UpstreamError
from packages.common.logging import get_logger
from packages.security import SecuritySettings

logger = get_logger("gateway.keycloak")


class KeycloakClient:
    """Keycloak 客户端：服务账号令牌与可达性探测。

    用户令牌的**校验**在 ``packages.security.identity``（走 JWKS，不依赖本模块）；
    本类只服务 eval / 定时任务等非交互场景。
    """

    def __init__(self, settings: SecuritySettings, timeout: float = 5.0) -> None:
        self._settings = settings
        self._timeout = timeout

    def _token_url(self) -> str:
        return f"{self._settings.issuer()}/protocol/openid-connect/token"

    async def client_credentials_token(self, client_secret: str) -> str:
        """服务账号令牌（client_credentials）。"""
        payload = {
            "grant_type": "client_credentials",
            "client_id": self._settings.keycloak_client_id,
            "client_secret": client_secret,
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(self._token_url(), data=payload)
                resp.raise_for_status()
                return str(resp.json()["access_token"])
        except Exception as exc:  # noqa: BLE001
            raise UpstreamError("keycloak", f"获取服务账号令牌失败: {exc}") from exc
