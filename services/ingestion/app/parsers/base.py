"""解析器抽象与注册表。

契约：解析结果 ``text`` 必须与后续 ``char_start/char_end`` 所在的坐标系一致。
对纯文本 / Markdown 而言就是**原始文件内容**，因此引用可以精确回查原文。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from packages.common.errors import ValidationError


@dataclass
class ParsedDocument:
    """解析结果：来源路径、标题与正文文本（坐标系须与 ``char_start/end`` 一致）。"""

    source: str
    title: str
    text: str


class BaseParser(ABC):
    """一种文件类型一个解析器。"""

    extensions: tuple[str, ...] = ()

    @abstractmethod
    def parse(self, path: Path) -> ParsedDocument: ...

    @staticmethod
    def _fallback_title(path: Path) -> str:
        return path.stem


_REGISTRY: dict[str, BaseParser] = {}


def register(parser: BaseParser) -> BaseParser:
    """按扩展名把解析器注册到全局表（幂等，重复注册覆盖）。"""
    for ext in parser.extensions:
        _REGISTRY[ext.lower()] = parser
    return parser


def get_parser(path: Path) -> BaseParser:
    """按扩展名取解析器；未注册扩展名抛 ``ValidationError``（提示已支持列表）。"""
    ext = path.suffix.lower()
    parser = _REGISTRY.get(ext)
    if parser is None:
        raise ValidationError(f"不支持的扩展名 {ext!r}（已注册: {sorted(_REGISTRY)}）")
    return parser


def supported_extensions() -> list[str]:
    """返回已注册扩展名（升序），供前端上传校验提示。"""
    return sorted(_REGISTRY)


def all_extensions() -> set[str]:
    return set(_REGISTRY)
