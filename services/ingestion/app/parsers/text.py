"""纯文本解析器：内容即原文，首行可作为标题。"""

from __future__ import annotations

from pathlib import Path

from app.parsers.base import BaseParser, ParsedDocument, register


class TextParser(BaseParser):
    """纯文本解析器：内容即原文，首行作标题（缺则回退文件名）。"""

    extensions = (".txt",)

    def parse(self, path: Path) -> ParsedDocument:
        text = path.read_text(encoding="utf-8", errors="replace")
        first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
        title = first_line[:120] if first_line else self._fallback_title(path)
        return ParsedDocument(source=str(path), title=title, text=text)


register(TextParser())
