"""PDF 解析器（pypdf 抽文本，不含 OCR）。

注意坐标系：``char_start/char_end`` 指向**抽取后的文本**，
而不是 PDF 字节流。引用回查时需要先按同样方式抽取文本。
扫描件（图片型 PDF）抽取结果为空，需接入 OCR——那是后续阶段的事，
此处直接给出可读的失败原因，而不是静默产出空文档。
"""

from __future__ import annotations

from pathlib import Path

from app.parsers.base import BaseParser, ParsedDocument, register
from packages.common.errors import ValidationError


class PdfParser(BaseParser):
    extensions = (".pdf",)

    def parse(self, path: Path) -> ParsedDocument:
        try:
            from pypdf import PdfReader
        except ImportError as exc:  # pragma: no cover
            raise ValidationError("未安装 pypdf，无法解析 PDF") from exc

        reader = PdfReader(str(path))
        pages = [(page.extract_text() or "") for page in reader.pages]
        text = "\n\n".join(pages).strip()
        if not text:
            raise ValidationError(f"{path.name} 未抽取到任何文本（可能是扫描件，需要 OCR）")

        title = ""
        try:
            meta = reader.metadata or {}
            title = (meta.get("/Title") or "").strip()
        except Exception:  # noqa: BLE001 - 元数据缺失不影响解析
            title = ""
        if not title:
            first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
            title = first_line[:120] or self._fallback_title(path)
        return ParsedDocument(source=str(path), title=title, text=text)


register(PdfParser())
