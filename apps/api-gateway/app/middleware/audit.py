"""审计中间件。

审计只记「谁在什么时候访问了哪个资源、结果如何」，
**不记录请求体与答案正文**——知识库问答往往携带敏感信息，
审计日志本身不应成为泄密面。需要排查内容问题请走 Langfuse trace。
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from packages.common.logging import get_logger

from app.config import Settings

logger = get_logger("gateway.audit")

# 这些路径量大且无审计价值
_QUIET_PATHS = {"/health", "/openapi.json", "/docs", "/redoc"}


class AuditMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, settings: Settings) -> None:  # noqa: ANN001
        super().__init__(app)
        self.enabled = settings.audit_enabled

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if not self.enabled:
            return await call_next(request)

        started = time.perf_counter()
        response = await call_next(request)
        elapsed = round((time.perf_counter() - started) * 1000, 1)

        if request.url.path in _QUIET_PATHS:
            return response

        identity = getattr(request.state, "identity", None)
        record = {
            "event": "http_access",
            "request_id": getattr(request.state, "request_id", ""),
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "duration_ms": elapsed,
            "tenant_id": getattr(identity, "tenant_id", ""),
            "user_id": getattr(identity, "user_id", ""),
            "client": request.client.host if request.client else "",
        }
        logger.info(json.dumps(record, ensure_ascii=False))
        return response
