"""租户限流中间件（固定窗口）。

为什么用固定窗口而不是令牌桶：Redis 上一条 ``INCR + EXPIRE`` 就够了，
无需 Lua 脚本；窗口边界的突发放行对知识库问答这种低频场景无实际影响。

可用性取向：Redis 不可用时**放行**（fail-open）。
限流是保护下游的手段，不是安全边界——真正越权由 OPA 拦截，
因为缓存故障把整个问答打挂才是更糟的结果。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from app.clients.redis import RedisCounter
from app.config import Settings
from packages.common.logging import get_logger

logger = get_logger("gateway.ratelimit")

WINDOW_SECONDS = 60


class RateLimitMiddleware(BaseHTTPMiddleware):
    """租户固定窗口限流中间件（Redis INCR+EXPIRE，无需 Lua 脚本）。

    Redis 不可用则 **fail-open** 放行：限流是保护下游的手段而非安全边界，
    真正越权由 OPA 拦截，因缓存故障把问答打挂更糟。
    """

    def __init__(self, app, settings: Settings, counter: RedisCounter) -> None:  # noqa: ANN001
        super().__init__(app)
        self.settings = settings
        self.counter = counter
        self.exempt = settings.rate_limit_exempt()

    @staticmethod
    def _key(tenant_id: str, window: int) -> str:
        return f"ratelimit:{tenant_id}:{window}"

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if not self.settings.rate_limit_enabled or request.url.path in self.exempt:
            return await call_next(request)

        identity = getattr(request.state, "identity", None)
        tenant_id = getattr(identity, "tenant_id", None) or "anonymous"
        window = int(time.time() // WINDOW_SECONDS)
        used = await self.counter.incr(self._key(tenant_id, window), WINDOW_SECONDS * 2)

        if used is not None and used > self.settings.rate_limit_per_minute:
            retry_after = WINDOW_SECONDS - int(time.time() % WINDOW_SECONDS)
            logger.warning("限流触发 tenant=%s used=%s", tenant_id, used)
            return JSONResponse(
                status_code=429,
                content={
                    "error": "RateLimited",
                    "message": f"租户 {tenant_id} 每分钟请求数超过 {self.settings.rate_limit_per_minute}",
                },
                headers={"retry-after": str(retry_after)},
            )

        return await call_next(request)
