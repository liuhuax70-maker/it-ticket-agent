"""导入即注册所有解析器。"""

from app.parsers import markdown, pdf, text  # noqa: F401
from app.parsers.base import (
    BaseParser,
    ParsedDocument,
    all_extensions,
    get_parser,
    register,
    supported_extensions,
)

__all__ = [
    "BaseParser",
    "ParsedDocument",
    "all_extensions",
    "get_parser",
    "register",
    "supported_extensions",
]
