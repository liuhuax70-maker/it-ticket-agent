"""文档切分与元数据抽取。

策略（对应 `开发流程/04-检索与编排设计.md` §2.3）：

1. **按标题/段落分块**：Markdown 标题变化即开启新块（结构感知），空行分段；
2. **超长块二次切分**：单块超过 token 预算时按句子再切，保证每块不超限；
3. **累加 + 重叠窗口**：相邻 chunk 之间保留约 `overlap` token 的上下文；
4. **元数据抽取**：正则抽取版本号（v1.2.3）与错误码（ERR-4041）写入标量字段；
5. **去重**：按内容哈希去重，避免重复片段污染召回。

token 数为**估算值**（无 tokenizer 依赖）：CJK 每字 1 个、英文/数字每词 1 个、标点按 0.5 个。
"""

import hashlib
import re
from dataclasses import dataclass, field

# ---- 抽取规则 ----
#: 版本号，如 v1.2.3 / 1.2
VERSION_RE = re.compile(r"\bv?\d+(?:\.\d+){1,3}\b")
#: 错误码，如 ERR-4041 / E4041 / ERR_4041
ERROR_CODE_RE = re.compile(r"\b(?:ERR|E)[-_]?\d{3,5}\b", re.IGNORECASE)

# ---- 切分规则 ----
#: Markdown 标题（1~6 级）
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*\S)\s*$")
#: 段落分隔（空行）
_SEG_SPLIT = re.compile(r"\n\s*\n")
#: 句子边界（保留标点）
_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;\n])")

# ---- token 估算规则 ----
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\u3040-\u30ff\uac00-\ud7af]")
_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
_PUNCT_RE = re.compile(r"[^\w\s]")

#: 默认切分参数（token 估算值）
DEFAULT_CHUNK_TOKENS = 400
DEFAULT_OVERLAP_TOKENS = 50


@dataclass
class RawChunk:
    """切分后的原始片段。"""

    doc_id: str
    chunk_index: int
    content: str
    title: str | None = None
    version: str | None = None
    error_code: str | None = None
    metadata: dict = field(default_factory=dict)

    @property
    def chunk_id(self) -> str:
        """片段唯一标识：`{doc_id}#{chunk_index}`。"""
        return f"{self.doc_id}#{self.chunk_index}"

    @property
    def content_hash(self) -> str:
        """内容哈希（用于去重）。"""
        normalized = re.sub(r"\s+", "", self.content)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数（不依赖 tokenizer）。"""
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    words = len(_WORD_RE.findall(text))
    punct = len(_PUNCT_RE.findall(text))
    return cjk + words + int(punct * 0.5)


def extract_metadata(text: str) -> dict:
    """从文本抽取版本号与错误码（取首个命中）。"""
    version = VERSION_RE.search(text)
    error_code = ERROR_CODE_RE.search(text)
    return {
        "version": version.group(0) if version else None,
        "error_code": error_code.group(0) if error_code else None,
    }


def _is_heading_only(text: str) -> bool:
    """判断文本是否只由标题行组成（无正文）。"""
    lines = [line for line in text.splitlines() if line.strip()]
    return bool(lines) and all(HEADING_RE.match(line) for line in lines)


def _flush_blocks(blocks: list, buf: list, heading: str | None, force: bool = False) -> None:
    """把缓冲区落成一个块。

    纯标题块（只有标题、没有正文）不落块，继续留在缓冲区与后续正文合并，
    避免产生「只有标题」的低价值 chunk。
    """
    content = "\n".join(buf).strip()
    if content and (force or not _is_heading_only(content)):
        blocks.append((heading, content))
        buf.clear()


def _split_blocks(text: str) -> list[tuple[str | None, str]]:
    """按 Markdown 标题与空行切分为 (所属标题, 文本) 列表。

    标题行本身会保留在其所属块的内容中，便于检索时命中。
    """
    blocks: list[tuple[str | None, str]] = []
    buf: list[str] = []
    current_heading: str | None = None

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        match = HEADING_RE.match(line)
        if match:
            _flush_blocks(blocks, buf, current_heading)
            current_heading = match.group(1).strip()
            buf.append(line)
        elif not line.strip():
            _flush_blocks(blocks, buf, current_heading)
        else:
            buf.append(line)

    _flush_blocks(blocks, buf, current_heading, force=True)
    return blocks


def _hard_split(text: str, max_tokens: int) -> list[str]:
    """按字符硬切（用于超长单句，CJK 近似 1 字 1 token）。"""
    step = max(1, max_tokens)
    return [piece.strip() for piece in (text[i : i + step] for i in range(0, len(text), step)) if piece.strip()]


def _split_long_text(text: str, max_tokens: int) -> list[str]:
    """将超长文本按句子切成不超过 max_tokens 的片段。"""
    if estimate_tokens(text) <= max_tokens:
        return [text.strip()] if text.strip() else []

    pieces: list[str] = []
    buf: list[str] = []
    buf_tokens = 0

    for sentence in _SENT_SPLIT.split(text):
        if not sentence or not sentence.strip():
            continue
        sent_tokens = estimate_tokens(sentence)

        if sent_tokens > max_tokens:  # 单句本身超限
            if buf:
                pieces.append("".join(buf).strip())
                buf, buf_tokens = [], 0
            pieces.extend(_hard_split(sentence, max_tokens))
            continue

        if buf and buf_tokens + sent_tokens > max_tokens:
            pieces.append("".join(buf).strip())
            buf, buf_tokens = [], 0

        buf.append(sentence)
        buf_tokens += sent_tokens

    if buf:
        pieces.append("".join(buf).strip())
    return [p for p in pieces if p]


def _char_tail(text: str, max_tokens: int) -> str:
    """按字符近似取尾部（CJK 近似 1 字 1 token），并尽量回退到句读边界。"""
    if len(text) <= max_tokens:
        return text.strip()
    tail = text[-max_tokens:]
    for sep in ("。", "！", "？", "；", "\n", ".", "!", "?", ";"):
        idx = tail.find(sep)
        if 0 <= idx < len(tail) - 1:
            return tail[idx + 1 :].strip()
    return tail.strip()


def _tail_by_tokens(text: str, overlap: int) -> str:
    """取文本尾部约 overlap token 的内容，作为下一个 chunk 的重叠上下文。

    优先按整段回退（可读性好）；若单段就超预算，则退化为按字符回退。
    """
    if overlap <= 0 or not text:
        return ""

    segments = [seg for seg in _SEG_SPLIT.split(text) if seg.strip()]
    picked: list[str] = []
    total = 0
    for seg in reversed(segments):
        seg_tokens = estimate_tokens(seg)
        if seg_tokens > overlap:
            # 单段就超出重叠预算：若还没选到段，按字符近似回退
            if not picked:
                return _char_tail(seg, overlap)
            break
        if total + seg_tokens > overlap:
            break
        picked.insert(0, seg)
        total += seg_tokens
        if total >= overlap:
            break

    if picked:
        return "\n\n".join(picked).strip()
    return _char_tail(text, overlap)


def split_document(
    doc_id: str,
    text: str,
    chunk_size: int = DEFAULT_CHUNK_TOKENS,
    overlap: int = DEFAULT_OVERLAP_TOKENS,
    title: str | None = None,
) -> list[RawChunk]:
    """将文档切分为带元数据的片段列表。

    :param doc_id: 文档标识（chunk_id 前缀）。
    :param text: 文档全文。
    :param chunk_size: 单块 token 预算（估算值）；叠加重叠后单块上限为 chunk_size + overlap。
    :param overlap: 相邻块之间的重叠 token 数（估算值）。
    :param title: 文档级标题，块内无标题时作为兜底 title。
    :raises ValueError: overlap 不小于 chunk_size 时。
    """
    if not text or not text.strip():
        return []
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap 必须满足 0 <= overlap < chunk_size")

    # 1) 标题/段落分块
    blocks = _split_blocks(text)

    # 2) 超长块按句子二次切分，保证单块不超预算
    units: list[tuple[str | None, str]] = []
    for heading, block_text in blocks:
        for piece in _split_long_text(block_text, chunk_size):
            units.append((heading, piece))

    # 3) 累加 + 重叠窗口
    #    说明：为让重叠上下文一定生效，单块上限取 chunk_size + overlap（软约束）。
    chunks: list[RawChunk] = []
    buf: list[str] = []
    buf_tokens = 0
    buf_heading: str | None = None
    index = 0

    def flush() -> None:
        nonlocal buf, buf_tokens, buf_heading, index
        content = "\n\n".join(buf).strip()
        chunk_title = buf_heading or title
        buf, buf_tokens, buf_heading = [], 0, None
        if not content:
            return
        meta = extract_metadata(content)
        chunks.append(
            RawChunk(
                doc_id=doc_id,
                chunk_index=index,
                content=content,
                title=chunk_title,
                version=meta["version"],
                error_code=meta["error_code"],
            )
        )
        index += 1

    for heading, unit_text in units:
        unit_tokens = estimate_tokens(unit_text)

        # 需要开启新块的两种情况：标题变化（结构感知切分）或超出 token 预算
        start_new_block = False
        if buf:
            if heading is not None and heading != buf_heading:
                start_new_block = True
            elif buf_tokens + unit_tokens > chunk_size:
                start_new_block = True

        if start_new_block:
            tail = _tail_by_tokens("\n\n".join(buf), overlap)
            flush()
            if tail:  # 上一块的尾部作为重叠上下文
                buf.append(tail)
                buf_tokens = estimate_tokens(tail)
            buf_heading = heading
        elif not buf:
            buf_heading = heading

        buf.append(unit_text)
        buf_tokens += unit_tokens

    flush()
    return chunks


def dedupe_chunks(chunks: list[RawChunk]) -> list[RawChunk]:
    """按内容哈希去重，保留首次出现的片段。"""
    seen: set[str] = set()
    result: list[RawChunk] = []
    for chunk in chunks:
        if chunk.content_hash in seen:
            continue
        seen.add(chunk.content_hash)
        result.append(chunk)
    return result
