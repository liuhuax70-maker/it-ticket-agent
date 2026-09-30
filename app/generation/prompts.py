"""Prompt 模板与上下文拼装。

约束目标（对应 `开发流程/01-需求与范围界定.md` 的质量要求）：

1. **只依据检索上下文作答**，不推理、不编造；
2. **数字 / 日期 / 版本号 / 错误码原样保留**，不得改写或换算；
3. **引用必须真实支撑结论**（引用回链校验见 `app/generation/citations.py`）；
4. **必须有「资料不足」出口**：允许回答「无法确定」，而不是硬编。

指令里只写约束，不写人设（人设占 token 不提质）。

## 上下文拼装

- **去重**：近重复片段重复出现会强化模型的错误确信（「多处提到 = 更可信」）；
- **显式预算**：超出 `context_max_chars` 时按排名丢弃并记录日志，
  而不是交给模型 API 静默截断（被截掉的可能正是答案所在片段）；
- **来源标注**：每段都带 `[来源: chunk_id]` 与来源类型，块内有标题层级路径。
"""

import hashlib
import re
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

SYSTEM_PROMPT = """你只能依据【知识片段】回答问题，遵守以下约束：
1. 只使用知识片段中的信息，不推理、不推测、不编造；
2. 数字、日期、版本号、错误码必须与原文完全一致，不得改写或换算；
3. 每条结论用 [来源: chunk_id] 标注依据，引用必须真实支撑该结论；
4. 若知识片段不足以回答问题，直接回答「根据现有资料无法确定」，并建议转人工，不要猜测。"""

USER_PROMPT_TEMPLATE = """【知识片段】
{context}

【用户问题】
{query}

要求：给出简洁、准确的处理步骤，并逐条标注 [来源: chunk_id]。
注意：以上片段可能来自不同文档；若彼此不一致，以片段自身的表述为准。"""

#: 知识库无命中时的回复
NOT_FOUND_REPLY = (
    "很抱歉，知识库中暂未找到与该问题相关的资料。"
    "为避免给出不准确的信息，建议将此工单转交人工处理。"
)

#: 生成服务不可用时的降级模板（直接摘录知识片段，交人工确认）
FALLBACK_TEMPLATE = """（生成服务暂时不可用，以下为知识库原文摘录，请人工确认后回复）

【问题】{query}

【相关片段】
{excerpts}"""

#: 近重复判定用的前缀长度
_NEAR_DUP_PREFIX = 120


def _field(chunk: Any, key: str, default: Any = None) -> Any:
    """兼容 dict 与 Pydantic 对象两种取值方式。"""
    if isinstance(chunk, dict):
        return chunk.get(key, default)
    return getattr(chunk, key, default)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _dedupe(chunks: list[Any]) -> list[Any]:
    """拼装前按「内容哈希 + 前 120 字」双重去重。"""
    seen_exact: set[str] = set()
    seen_near: set[str] = set()
    result: list[Any] = []

    for chunk in chunks:
        content = _field(chunk, "content", "") or ""
        normalized = _normalize(content)
        exact = _field(chunk, "content_hash") or hashlib.sha256(
            normalized.encode("utf-8")
        ).hexdigest()
        near = normalized[:_NEAR_DUP_PREFIX]
        if exact in seen_exact or near in seen_near:
            continue
        seen_exact.add(exact)
        seen_near.add(near)
        result.append(chunk)
    return result


def _format_block(chunk: Any) -> str:
    """单段格式：带来源与来源类型；块正文自带头标题层级路径。"""
    chunk_id = _field(chunk, "chunk_id", "")
    source = _field(chunk, "source", "")
    source_value = getattr(source, "value", source)
    content = _field(chunk, "content", "") or ""
    return f"[来源: {chunk_id}]（{source_value}）\n{content}"


def build_context(chunks: list[Any], *, max_chars: int | None = None) -> str:
    """把检索片段拼装为上下文文本（去重 + 显式预算）。"""
    if not chunks:
        return ""

    settings = get_settings()
    budget = max_chars if max_chars is not None else settings.context_max_chars

    blocks: list[str] = []
    used = 0
    dropped: list[str] = []

    for chunk in _dedupe(chunks):
        block = _format_block(chunk)
        if used + len(block) > budget:
            dropped.append(_field(chunk, "chunk_id", ""))
            continue
        blocks.append(block)
        used += len(block)

    if dropped:
        logger.warning(
            "上下文预算 %d 字符不足，已按排名丢弃 %d 个片段（被丢弃的可能含答案，需关注）: %s",
            budget,
            len(dropped),
            dropped,
        )

    return "\n\n---\n\n".join(blocks)


def build_fallback(query: str, chunks: list[Any], limit: int = 3) -> str:
    """生成降级回复：直接摘录 Top-N 片段原文。"""
    excerpts = []
    for chunk in _dedupe(chunks)[:limit]:
        excerpts.append(f"[{_field(chunk, 'chunk_id', '')}] {_field(chunk, 'content', '')}")
    return FALLBACK_TEMPLATE.format(query=query, excerpts="\n\n".join(excerpts) or "（无）")
