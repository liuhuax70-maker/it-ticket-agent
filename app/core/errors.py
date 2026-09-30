"""业务错误码与统一异常处理。

错误码与 `开发流程/05-接口与数据契约设计.md` 的 §6 错误码表保持一致。
所有异常最终都转换为统一响应体：
    {"code": int, "message": str, "trace_id": str, "data": null, "detail": ...}
"""

import logging
from enum import IntEnum

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


class ErrorCode(IntEnum):
    """业务错误码。"""

    OK = 0
    INVALID_PARAM = 1001
    UNAUTHORIZED = 1002
    FORBIDDEN = 1003
    NOT_FOUND = 1004
    CONFLICT = 1005
    GONE = 1006
    VECTOR_SERVICE_UNAVAILABLE = 2001
    GENERATION_SERVICE_UNAVAILABLE = 2002
    TIMEOUT = 2003
    GRAPH_ERROR = 2004
    RATE_LIMITED = 3001
    UNKNOWN = 9000


#: 业务错误码 -> HTTP 状态码
_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.OK: 200,
    ErrorCode.INVALID_PARAM: 400,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.CONFLICT: 409,
    ErrorCode.GONE: 410,
    ErrorCode.VECTOR_SERVICE_UNAVAILABLE: 502,
    ErrorCode.GENERATION_SERVICE_UNAVAILABLE: 502,
    ErrorCode.TIMEOUT: 504,
    ErrorCode.GRAPH_ERROR: 500,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.UNKNOWN: 500,
}


class AppError(Exception):
    """业务异常：携带错误码，由全局处理器转换为统一响应。"""

    def __init__(self, code: ErrorCode, message: str, detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail
        self.http_status = _HTTP_STATUS.get(code, 500)


def _payload(code: int, message: str, trace_id: str | None, detail=None) -> dict:
    return {
        "code": int(code),
        "message": message,
        "trace_id": trace_id,
        "data": None,
        "detail": detail,
    }


def _trace_id(request: Request) -> str | None:
    return getattr(request.state, "trace_id", None)


def register_exception_handlers(app: FastAPI) -> None:
    """注册全局异常处理器。"""

    @app.exception_handler(AppError)
    async def _app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        logger.warning("AppError code=%s msg=%s", exc.code, exc.message)
        return JSONResponse(
            status_code=exc.http_status,
            content=_payload(exc.code, exc.message, _trace_id(request), exc.detail),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=_payload(
                ErrorCode.INVALID_PARAM,
                "参数校验失败",
                _trace_id(request),
                {"errors": exc.errors()},
            ),
        )

    @app.exception_handler(Exception)
    async def _unknown_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error: %s", exc)
        return JSONResponse(
            status_code=500,
            content=_payload(ErrorCode.UNKNOWN, "服务内部错误", _trace_id(request)),
        )
