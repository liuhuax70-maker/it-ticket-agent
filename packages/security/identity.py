"""身份解析与 ACL 过滤契约编译。

最小闭环（AUTHZ_ENABLED=false）返回**固定身份**，但过滤契约照常编译并下传到
retrieval —— 这样 P2 打开鉴权时，只需换掉 ``resolve_identity`` 的实现，
网关、编排器、检索服务的代码一行都不用改。
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import Request
from pydantic import BaseModel, Field

from packages.common.errors import RagError
from packages.common.logging import get_logger
from packages.contracts import ACL, Visibility
from packages.security.config import SecuritySettings

logger = get_logger("security.identity")


class Identity(BaseModel):
    user_id: str = "u_demo"
    tenant_id: str = "default"
    department_id: str = "default"
    roles: list[str] = Field(default_factory=list)
    email: str | None = None
    is_admin: bool = False

    def to_acl(self, *, owner: bool = True) -> ACL:
        """转成请求方 ACL。

        ``owner=True`` 时携带 user_id，使 ``private`` 可见性分支生效
        （用户能检索到自己的私有文档）。
        """
        return ACL(
            tenant_id=self.tenant_id,
            department_id=self.department_id,
            visibility=Visibility.internal,
            owner=self.user_id if owner else None,
        )


class Unauthorized(RagError):
    status_code = 401


# --------------------------------------------------------------------------
# 身份解析
# --------------------------------------------------------------------------

_jwks_cache: dict[str, Any] = {"fetched_at": 0.0, "keys": []}
JWKS_TTL_SECONDS = 3600


def _fetch_jwks(settings: SecuritySettings) -> list[dict[str, Any]]:
    now = time.time()
    if _jwks_cache["keys"] and now - _jwks_cache["fetched_at"] < JWKS_TTL_SECONDS:
        return _jwks_cache["keys"]
    import httpx

    resp = httpx.get(settings.jwks_url(), timeout=5.0)
    resp.raise_for_status()
    keys = resp.json().get("keys", [])
    _jwks_cache.update({"fetched_at": now, "keys": keys})
    return keys


def decode_keycloak_token(token: str, settings: SecuritySettings) -> dict[str, Any]:
    """校验并解析 Keycloak JWT，失败抛 ``Unauthorized``。"""
    from jose import jwt
    from jose.exceptions import JWTError

    keys = _fetch_jwks(settings)
    if not keys:
        raise Unauthorized("Keycloak JWKS 为空，无法校验令牌")

    try:
        header = jwt.get_unverified_header(token)
    except JWTError as exc:
        raise Unauthorized(f"令牌格式非法: {exc}") from exc

    kid = header.get("kid")
    jwk_dict = next((k for k in keys if k.get("kid") == kid), keys[0])
    kwargs: dict[str, Any] = {"algorithms": ["RS256"], "issuer": settings.issuer()}
    audience = settings.keycloak_audience or settings.keycloak_client_id
    if audience:
        kwargs["audience"] = audience

    try:
        return jwt.decode(token, jwk_dict, **kwargs)
    except JWTError as exc:
        raise Unauthorized(f"令牌校验失败: {exc}") from exc


def resolve_identity(request: Request, settings: SecuritySettings) -> Identity:
    """从请求解析身份；AUTHZ_ENABLED=false 时返回固定身份。"""
    if not settings.authz_enabled:
        return Identity(
            user_id=settings.default_user_id,
            tenant_id=settings.default_tenant_id,
            department_id=settings.default_department_id,
            roles=["rag_user"],
        )

    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise Unauthorized("缺少 Bearer 令牌")
    claims = decode_keycloak_token(auth.split(" ", 1)[1].strip(), settings)
    roles = claims.get("realm_access", {}).get("roles", [])
    return Identity(
        user_id=claims.get("sub", "unknown"),
        tenant_id=claims.get("tenant_id") or settings.default_tenant_id,
        department_id=claims.get("department_id") or settings.default_department_id,
        roles=roles,
        email=claims.get("email"),
        is_admin="rag_admin" in roles,
    )


# 说明：ACL -> 过滤契约的编译只此一处实现，位于
# ``packages.retrievers.filters.compile_filters``；此处只负责把请求解析成 Identity。
