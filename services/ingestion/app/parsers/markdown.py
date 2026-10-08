"""Markdown 解析器：保留原文，标题取第一个一级标题。"""

from __future__ import annotations

import re
from pathlib import Path

from app.parsers.base import BaseParser, ParsedDocument, register

_H1 = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


class MarkdownParser(BaseParser):
    """Markdown 解析器：保留原文，标题取自首个一级标题（缺则回退文件名）。"""

    extensions = (".md", ".markdown")

    def parse(self, path: Path) -> ParsedDocument:
        text = path.read_text(encoding="utf-8")
        match = _H1.search(text)
        title = match.group(1).strip() if match else self._fallback_title(path)
        return ParsedDocument(source=str(path), title=title, text=text)


register(MarkdownParser())
