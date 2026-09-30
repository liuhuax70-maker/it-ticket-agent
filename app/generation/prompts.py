"""Prompt 模板与上下文拼装。

约束目标（对应 `开发流程/01-需求与范围界定.md` 的质量要求）：
1. 只依据检索上下文作答，不臆造；
2. 必须标注引用来源，便于人工核对；
3. 上下文不足时明确说明并建议转人工。
"""

from typing import Any

SYSTEM_PROMPT = """你是企业内部 IT/客服支持助手。
请严格依据【知识片段】回答用户问题，遵守：
1. 只使用知识片段中的信息，不得编造；
2. 回答末尾以 [来源: chunk_id] 形式标注引用；
3. 若知识片段不足以回答，明确说明「未找到相关知识」并建议转人工。"""

USER_PROMPT_TEMPLATE = """【知识片段】
{context}

【用户问题】
{query}

请给出简洁、准确的处理步骤，并标注引用来源。"""

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


def _field(chunk: Any, key: str, default: Any = None) -> Any:
    """兼容 dict 与 Pydantic 对象两种取值方式。"""
    if isinstance(chunk, dict):
        return chunk.get(key, default)
    return getattr(chunk, key, default)


def build_context(chunks: list[Any]) -> str:
    """把检索片段拼装为上下文文本。"""
    blocks: list[str] = []
    for chunk in chunks:
        chunk_id = _field(chunk, "chunk_id", "")
        title = _field(chunk, "title") or ""
        content = _field(chunk, "content", "")
        blocks.append(f"[{chunk_id}] {title}".rstrip() + f"\n{content}")
    return "\n\n---\n\n".join(blocks)


def build_fallback(query: str, chunks: list[Any], limit: int = 3) -> str:
    """生成降级回复：直接摘录 Top-N 片段原文。"""
    excerpts = []
    for chunk in chunks[:limit]:
        chunk_id = _field(chunk, "chunk_id", "")
        excerpts.append(f"[{chunk_id}] {_field(chunk, 'content', '')}")
    return FALLBACK_TEMPLATE.format(query=query, excerpts="\n\n".join(excerpts) or "（无）")
