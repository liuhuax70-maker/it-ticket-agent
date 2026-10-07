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
    for ext in parser.extensions:
        _REGISTRY[ext.lower()] = parser
    return parser


def get_parser(path: Path) -> BaseParser:
    ext = path.suffix.lower()
    parser = _REGISTRY.get(ext)
    if parser is None:
        raise ValidationError(
            f"不支持的扩展名 {ext!r}（已注册: {sorted(_REGISTRY)}）"
        )
    return parser


def supported_extensions() -> list[str]:
    return sorted(_REGISTRY)


def all_extensions() -> set[str]:
    return set(_REGISTRY)
