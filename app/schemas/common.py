"""通用响应模型。"""

from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    """统一响应封装（非流式接口）。"""

    code: int = 0
    message: str = "ok"
    trace_id: str | None = None
    data: T | None = None


class ErrorInfo(BaseModel):
    """统一错误体。"""

    code: int
    message: str
    detail: dict | None = None
    trace_id: str | None = None
