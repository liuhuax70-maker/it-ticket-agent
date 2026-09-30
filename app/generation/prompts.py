"""Prompt 模板。

约束目标（对应 `开发流程/01-需求与范围界定.md` 的质量要求）：
1. 只依据检索上下文作答，不臆造；
2. 必须给出引用来源，便于人工核对；
3. 上下文不足时明确说明并建议转人工。
"""

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


def build_context(chunks) -> str:
    """将检索片段拼装为上下文文本。"""
    blocks = []
    for c in chunks:
        head = f"[{c.chunk_id}] {c.title or ''}".strip()
        blocks.append(f"{head}\n{c.content}")
    return "\n\n---\n\n".join(blocks)
