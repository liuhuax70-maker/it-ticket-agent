"""编排状态模型。

对应 `开发流程/04-检索与编排设计.md` §3.1 的 TicketState，
整体由 Checkpointer 持久化，以 session_id 作为图线程 ID（thread_id）。

注意：`retrieved` 存放的是**普通 dict**（`Chunk.model_dump()`）而不是 Pydantic 对象，
原因是要让整个 State 可被 Checkpointer 稳定序列化，也便于直接上报观测系统。
"""

from typing import Any, TypedDict


class TicketState(TypedDict, total=False):
    """图状态：节点间传递的唯一数据载体。"""

    # ---- 输入 ----
    session_id: str
    ticket_id: str
    query: str
    #: 多轮历史；由接入层转成普通 dict 传入（保证状态可 JSON 序列化）
    history: list[dict[str, Any]]

    # ---- 中间产物 ----
    intent: str
    need_review: bool
    retrieved: list[dict[str, Any]]
    retrieval_debug: dict[str, Any]
    draft: str
    citations: list[str]

    # ---- 审核与发送 ----
    review_status: str
    reviewer_comment: str
    edited_reply: str | None
    reply: str
    send_status: str
    error: str
