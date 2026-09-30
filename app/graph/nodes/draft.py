"""草稿生成节点：基于检索上下文调用本地生成模型。

**流式与否由运行配置决定**，而不是靠探测流上下文：

- 调用方在 `config.configurable.stream_tokens = True` 时（SSE 接口会设置），
  节点用 `generate_stream()` 逐 token 通过 `get_stream_writer()` 推送
  `{"event": "token", "delta": ...}`，供 SSE 实时下发；
- 否则走非流式 `generate()`。

> 为什么不直接判断流上下文：`get_stream_writer()` 在非流式 `invoke` 下**也会返回一个可调用的
> writer**（只是没人消费），据此判断会让「非流式调用」也走逐 token 分支，
> 表现为单元测试静默调用真实模型。

**降级**：生成服务不可用时，返回「知识片段原文摘录」模板而非报错（见 `开发流程/04` §3.7）。
"""

from collections.abc import Callable

from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer

from app.core.errors import AppError
from app.core.logging import get_logger
from app.generation.ollama import generate, generate_stream
from app.generation.prompts import (
    SYSTEM_PROMPT,
    USER_PROMPT_TEMPLATE,
    build_context,
    build_fallback,
)
from app.graph.state import TicketState

logger = get_logger(__name__)


def _get_writer() -> Callable[[object], None] | None:
    """取自定义流写入器；不在可流式上下文时返回 None。"""
    try:
        return get_stream_writer()
    except Exception:  # noqa: BLE001 - 取不到写入器是正常场景
        return None


def _stream_enabled(config: RunnableConfig | None) -> bool:
    """是否启用逐 token 流式（由调用方在 config 中显式声明）。"""
    if not config:
        return False
    return bool((config.get("configurable") or {}).get("stream_tokens"))


def _stream_draft(system: str, user: str, writer: Callable[[object], None]) -> str:
    """流式生成并逐段推送 token，返回完整草稿。"""
    parts: list[str] = []
    for delta in generate_stream(system, user):
        parts.append(delta)
        writer({"event": "token", "delta": delta})
    return "".join(parts)


def draft_node(state: TicketState, config: RunnableConfig | None = None) -> dict:
    """生成回复草稿，并回填引用列表。"""
    query = state.get("query", "")
    chunks = state.get("retrieved") or []
    citations = [chunk.get("chunk_id", "") for chunk in chunks]

    context = build_context(chunks)
    user_prompt = USER_PROMPT_TEMPLATE.format(context=context, query=query)

    writer = _get_writer() if _stream_enabled(config) else None
    try:
        if writer is not None:
            draft = _stream_draft(SYSTEM_PROMPT, user_prompt, writer)
        else:
            draft = generate(SYSTEM_PROMPT, user_prompt)
    except AppError as exc:
        logger.warning("草稿生成失败，降级为片段摘录模板: %s", exc)
        return {
            "draft": build_fallback(query, chunks),
            "citations": citations,
            "error": f"draft_fallback: {exc}",
        }

    return {"draft": draft, "citations": citations}
