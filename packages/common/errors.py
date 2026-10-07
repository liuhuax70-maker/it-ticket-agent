"""统一错误类型与 FastAPI 异常处理。"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class RagError(Exception):
    """业务错误基类。"""

    status_code = 500

    def __init__(self, message: str, *, detail: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}


class ConfigError(RagError):
    status_code = 500


class NotFoundError(RagError):
    status_code = 404


class Forbidden(RagError):
    status_code = 403


class ValidationError(RagError):
    status_code = 422


class UpstreamError(RagError):
    """下游服务调用失败（retrieval / model-gateway / indexing ...）。"""

    status_code = 502

    def __init__(self, service: str, message: str, *, detail: dict | None = None) -> None:
        super().__init__(f"[{service}] {message}", detail=detail)
        self.service = service


class DependencyUnavailable(RagError):
    """依赖组件不可用（Milvus / OpenSearch / Redis / Postgres）。"""

    status_code = 503


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(RagError)
    async def _handle(request: Request, exc: RagError) -> JSONResponse:  # noqa: ARG001
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": type(exc).__name__, "message": exc.message, "detail": exc.detail},
        )
