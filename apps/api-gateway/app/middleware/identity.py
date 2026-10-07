"""身份中间件与授权依赖。

职责分离：
    * ``IdentityMiddleware`` **只做解析**（JWT -> Identity），失败即 401；
    * 授权（OPA）放在需要它的路由上，用 ``require_action`` / ``require_admin``，
      避免「所有请求都过一次策略引擎」这种既慢又说不清在保护什么的做法。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from app.config import Settings
from packages.common.errors import Forbidden, RagError
from packages.common.ids import new_id
from packages.common.logging import get_logger
from packages.security import Identity, resolve_identity
from packages.security.identity import Unauthorized

logger = get_logger("gateway.identity")


class IdentityMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, settings: Settings) -> None:  # noqa: ANN001
        super().__init__(app)
        self.settings = settings

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("x-request-id") or new_id("req_")
        request.state.request_id = request_id
        try:
            identity = resolve_identity(request, self.settings)
        except RagError as exc:
            logger.warning("鉴权失败 path=%s err=%s", request.url.path, exc.message)
            return JSONResponse(
                status_code=exc.status_code,
                content={"error": type(exc).__name__, "message": exc.message},
                headers={"x-request-id": request_id},
            )

        request.state.identity = identity
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        return response


def get_identity(request: Request) -> Identity:
    identity = getattr(request.state, "identity", None)
    if identity is None:  # pragma: no cover - 中间件缺失时才可能发生
        raise Unauthorized("身份未注入，请检查 IdentityMiddleware 是否已挂载")
    return identity


def require_action(action: str) -> Callable[[Request], Awaitable[Identity]]:
    """OPA 决策依赖。鉴权未启用时直接放行（最小闭环）。"""

    async def _dep(request: Request) -> Identity:
        identity = get_identity(request)
        opa = getattr(request.app.state, "opa", None)
        if opa is None or not opa.enabled:
            return identity

        allowed, reason = await opa.allow(
            {
                "action": action,
                "user": identity.model_dump(),
                "resource": {"type": "rag", "path": request.url.path},
            }
        )
        if not allowed:
            logger.warning("策略拒绝 user=%s action=%s reason=%s", identity.user_id, action, reason)
            raise Forbidden(f"策略拒绝: {reason}")
        return identity

    return _dep


def require_admin(request: Request) -> Identity:
    identity = get_identity(request)
    if getattr(request.app.state, "opa", None) is not None and getattr(
        request.app.state.opa, "enabled", False
    ):
        if not identity.is_admin:
            raise Forbidden("需要 rag_admin 角色")
    return identity
