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

from packages.common.errors import DependencyUnavailable, RagError
from packages.common.logging import get_logger
from packages.contracts import ACL, Visibility
from packages.security.config import SecuritySettings

logger = get_logger("security.identity")


class Identity(BaseModel):
    """解析出的请求方身份。

    最小闭环下由 ``resolve_identity`` 返回固定值；打开鉴权时由 Keycloak 声明填充。
    它是 ACL 的唯一来源（``to_acl`` 据此编译过滤契约），**绝不能由客户端指定**。
    """

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
    """未认证（401）：缺令牌 / 令牌格式非法 / 验签失败。

    注意它是 RagError 家族——中间件统一转成 JSON 响应，不会泄漏栈信息。
    """

    status_code = 401


# --------------------------------------------------------------------------
# 身份解析
# --------------------------------------------------------------------------

# 进程级 JWKS 缓存，按 jwks_url 分键：同一进程服务多个 realm / 多 Keycloak 实例时
# 不会串用公钥（否则 A realm 的公钥会被拿去验 B realm 的令牌，多租户验签结果不可信）。
# {url: {"fetched_at": float, "keys": [...]}}
#
# ⚠️ 仍是**进程内**缓存，多 worker 各存一份；轮换密钥时不同 worker 的刷新时刻不同，
# 因此下面 decode 失败还会强制刷新一次（见 decode_keycloak_token）。
_jwks_cache: dict[str, dict[str, Any]] = {}
# 短 TTL + 验签失败强制刷新：Keycloak 轮换签名密钥后，长缓存会让所有令牌验签失败
JWKS_TTL_SECONDS = 300


def _fetch_jwks(settings: SecuritySettings, *, force: bool = False) -> list[dict[str, Any]]:
    """取 JWKS 公钥列表，命中同一 ``jwks_url`` 且 TTL 内的缓存则直接返回。

    注意这里用的是**同步** httpx，而调用链（``resolve_identity``）是 async：
    缓存未命中时会在事件循环里阻塞最多 5 秒（timeout）。JWKS 每 5 分钟才刷一次，
    正常情况下影响有限；但 Keycloak 不可达时，每个请求都可能付这 5 秒。
    要彻底解决需改为 httpx.AsyncClient，并把本函数一并改成 async。

    网络错误必须转成 :class:`DependencyUnavailable`（RagError 家族）：
    中间件只捕 RagError，裸的 httpx 异常会把"身份服务不可达"变成
    500 Internal Server Error——监控会把 IdP 故障误判成网关 bug。
    """
    url = settings.jwks_url()
    now = time.time()
    entry = _jwks_cache.get(url)
    if (
        not force
        and entry
        and entry["keys"]
        and now - entry["fetched_at"] < JWKS_TTL_SECONDS
    ):
        return entry["keys"]
    import httpx

    try:
        resp = httpx.get(url, timeout=5.0)
        resp.raise_for_status()
        keys = resp.json().get("keys", [])
    except httpx.HTTPError as exc:
        # 连接失败/超时/5xx：这是依赖故障不是"令牌非法"，语义上属于 503
        raise DependencyUnavailable("keycloak", f"JWKS 拉取失败: {exc}") from exc
    _jwks_cache[url] = {"fetched_at": now, "keys": keys}
    return keys


def _decode_with_keys(
    token: str, settings: SecuritySettings, keys: list[dict[str, Any]]
) -> dict[str, Any]:
    """用给定公钥集合验签并解析声明。

    两处刻意保留的宽松处理，改动前请确认影响：

    * ``kid`` 未命中时回退到 ``keys[0]``：部分 IdP 发的令牌不带 kid，此时只能试第一个。
      代价是**可能用错误的密钥验签**（多 key 场景）；安全边界靠 issuer/audience 校验兜底
      （``keycloak_verify_issuer`` 默认开），不是靠 kid 精确匹配。
    * ``algorithms=["RS256"]`` 硬编码：IdP 换成 ES256/PS256 时会全量 401，
      报的是"令牌校验失败"，排查方向容易走偏。要支持多算法必须显式加白名单，
      不要改成"接受所有"（那会引入 alg=none 类风险）。
    """
    from jose import jwt
    from jose.exceptions import JWTError

    try:
        header = jwt.get_unverified_header(token)
    except JWTError as exc:
        raise Unauthorized(f"令牌格式非法: {exc}") from exc

    kid = header.get("kid")
    jwk_dict = next((k for k in keys if k.get("kid") == kid), keys[0])
    kwargs: dict[str, Any] = {"algorithms": ["RS256"]}
    if settings.keycloak_verify_issuer:
        kwargs["issuer"] = settings.issuer()
    audience = settings.keycloak_audience or settings.keycloak_client_id
    if audience:
        kwargs["audience"] = audience

    try:
        return jwt.decode(token, jwk_dict, **kwargs)
    except JWTError as exc:
        raise Unauthorized(f"令牌校验失败: {exc}") from exc


def decode_keycloak_token(token: str, settings: SecuritySettings) -> dict[str, Any]:
    """校验并解析 Keycloak JWT，失败抛 ``Unauthorized``。

    策略：先按缓存校验；失败则**强制刷新一次 JWKS 再试**。
    没有这一步，Keycloak 轮换签名密钥（重建、升级、多副本）后，
    在缓存过期前所有令牌都会被误判为非法——表现为"刚登录就 401"。
    """
    keys = _fetch_jwks(settings)
    if not keys:
        raise Unauthorized("Keycloak JWKS 为空，无法校验令牌")
    if not settings.keycloak_verify_issuer:
        logger.warning("KEYCLOAK_VERIFY_ISSUER=false：跳过 issuer 校验，仅限开发环境")

    try:
        return _decode_with_keys(token, settings, keys)
    except Unauthorized as first_error:
        logger.info("令牌校验失败，强制刷新 JWKS 后重试一次: %s", first_error.message)
        refreshed = _fetch_jwks(settings, force=True)
        if not refreshed:
            raise
        return _decode_with_keys(token, settings, refreshed)


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
