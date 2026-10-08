"""标题感知 + 偏移精确的递归切分。

两层结构：
    1. 先按 Markdown 标题扫描，构建标题栈，得到「章节 -> 字符区间 + 层级路径」；
    2. 章节正文再按中英文分隔符递归细分为原子，再贪心装箱成 chunk。

与直接用 RecursiveCharacterTextSplitter 的区别：本实现**自持字符偏移**，
每个 chunk 的 ``char_start/char_end`` 与原文逐字对齐（可直接回查原文），
而不是切完再用 find() 反推（重叠片段会让 find 找到错误位置）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.chunkers.base import BaseChunker, ChunkingConfig, estimate_tokens
from packages.common.ids import stable_chunk_id
from packages.contracts import Chunk, Document

# 标题：最多 6 级，标题后至少一个空格
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$", re.MULTILINE)

# 由粗到细：段落 -> 换行 -> 中文句末 -> 中文分句 -> 空格 -> 硬切
_SEPARATORS: tuple[str, ...] = ("\n\n", "\n", "。", "！", "？", "；", "，", " ", "")


@dataclass
class _Section:
    start: int
    end: int
    path: list[str]


def _build_sections(content: str, doc_title: str) -> list[_Section]:
    matches = list(_HEADING.finditer(content))
    if not matches:
        return [_Section(0, len(content), [doc_title])]

    sections: list[_Section] = []
    if matches[0].start() > 0:  # 首个标题之前的引言
        sections.append(_Section(0, matches[0].start(), [doc_title]))

    stack: list[tuple[int, str]] = []
    for i, match in enumerate(matches):
        level = len(match.group(1))
        title = match.group(2).strip()
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        body_start = match.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        trail = [t for _, t in stack if t]
        # 文档一级标题常与 doc_title 相同，避免路径出现「员工手册 > 员工手册」
        if trail and doc_title and trail[0].strip() == doc_title.strip():
            path = trail
        else:
            path = [p for p in [doc_title, *trail] if p]
        sections.append(_Section(body_start, body_end, path))
    return sections


def _atoms(
    text: str, base: int, separators: tuple[str, ...], max_atom: int
) -> list[tuple[int, int]]:
    """把文本切成不超过 max_atom 的连续片段区间。"""
    if not text:
        return []
    for i, sep in enumerate(separators):
        if sep and sep in text:
            parts = text.split(sep)
            spans: list[tuple[int, int]] = []
            cursor = base
            for j, part in enumerate(parts):
                piece = part + (sep if j < len(parts) - 1 else "")
                if not piece:
                    continue
                if len(piece) <= max_atom:
                    spans.append((cursor, cursor + len(piece)))
                else:
                    spans.extend(_atoms(piece, cursor, separators[i + 1 :], max_atom))
                cursor += len(piece)
            return spans

    # 已无分隔符可用：按 max_atom 硬切
    spans = []
    cursor = base
    while cursor < base + len(text):
        end = min(cursor + max_atom, base + len(text))
        spans.append((cursor, end))
        cursor = end
    return spans


def _pack(atoms: list[tuple[int, int]], chunk_size: int, overlap: int) -> list[tuple[int, int]]:
    """贪心装箱：尽量填满 chunk_size，块间保留 overlap。"""
    if not atoms:
        return []

    packed: list[list[tuple[int, int]]] = []
    current: list[tuple[int, int]] = []
    current_len = 0

    for atom in atoms:
        atom_len = atom[1] - atom[0]
        if current and current_len + atom_len > chunk_size:
            packed.append(current)
            # 用尾部片段做重叠，保证语义不断裂
            tail: list[tuple[int, int]] = []
            tail_len = 0
            for prev in reversed(current):
                if tail_len >= overlap:
                    break
                tail.insert(0, prev)
                tail_len += prev[1] - prev[0]
            current = tail
            current_len = tail_len
        current.append(atom)
        current_len += atom_len

    if current:
        packed.append(current)

    spans = [(group[0][0], group[-1][1]) for group in packed]
    return spans


def _tighten(content: str, start: int, end: int) -> tuple[int, int]:
    """收掉首尾空白，保证 content[start:end] 与展示文本逐字一致。"""
    while start < end and content[start].isspace():
        start += 1
    while end > start and content[end - 1].isspace():
        end -= 1
    return start, end


class RecursiveChunker(BaseChunker):
    """标题感知 + 偏移精确的递归切分器。

    先按 Markdown 标题分层（得到章节路径），正文再按中英文分隔符递归细分后贪心装箱；
    每个 chunk 的 ``char_start/char_end`` 与原文逐字对齐，可直接回查原文（而非切完再 find）。
    """

    def __init__(self, config: ChunkingConfig | None = None) -> None:
        self.config = config or ChunkingConfig()

    def chunk(self, doc: Document) -> list[Chunk]:
        cfg = self.config
        content = doc.content
        chunks: list[Chunk] = []
        index = 0

        for section in _build_sections(content, doc.title):
            body = content[section.start : section.end]
            if not body.strip():
                continue
            atoms = _atoms(body, section.start, _SEPARATORS, cfg.chunk_size)
            for raw_start, raw_end in _pack(atoms, cfg.chunk_size, cfg.chunk_overlap):
                start, end = _tighten(content, raw_start, raw_end)
                if end <= start:
                    continue
                text = content[start:end]
                chunks.append(
                    Chunk(
                        chunk_id=stable_chunk_id(doc.doc_id, index),
                        doc_id=doc.doc_id,
                        text=text,
                        chunk_index=index,
                        char_start=start,
                        char_end=end,
                        section_path=" > ".join(section.path),
                        doc_title=doc.title,
                        source=doc.source,
                        token_count=estimate_tokens(text),
                        acl=doc.acl,
                    )
                )
                index += 1

        # 文档极短时（如只有标题行）兜底产出一个 chunk，避免整篇丢空
        if not chunks and content.strip():
            start, end = _tighten(content, 0, len(content))
            chunks.append(
                Chunk(
                    chunk_id=stable_chunk_id(doc.doc_id, 0),
                    doc_id=doc.doc_id,
                    text=content[start:end],
                    chunk_index=0,
                    char_start=start,
                    char_end=end,
                    section_path=doc.title,
                    doc_title=doc.title,
                    source=doc.source,
                    token_count=estimate_tokens(content[start:end]),
                    acl=doc.acl,
                )
            )
        return chunks
