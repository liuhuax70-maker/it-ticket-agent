"""草稿生成节点：基于检索上下文调用本地生成模型。

降级：生成服务不可用时，返回「知识片段原文摘录」模板而非报错（见 `开发流程/04` §3.7）。
"""

from app.core.errors import AppError
from app.core.logging import get_logger
from app.generation.ollama import generate
from app.generation.prompts import (
    SYSTEM_PROMPT,
    USER_PROMPT_TEMPLATE,
    build_context,
    build_fallback,
)
from app.graph.state import TicketState

logger = get_logger(__name__)


def draft_node(state: TicketState) -> dict:
    """生成回复草稿，并回填引用列表。"""
    query = state.get("query", "")
    chunks = state.get("retrieved") or []
    citations = [chunk.get("chunk_id", "") for chunk in chunks]

    context = build_context(chunks)
    user_prompt = USER_PROMPT_TEMPLATE.format(context=context, query=query)

    try:
        draft = generate(SYSTEM_PROMPT, user_prompt)
    except AppError as exc:
        logger.warning("草稿生成失败，降级为片段摘录模板: %s", exc)
        return {
            "draft": build_fallback(query, chunks),
            "citations": citations,
            "error": f"draft_fallback: {exc}",
        }

    return {"draft": draft, "citations": citations}
