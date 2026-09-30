"""编排状态模型。

对应 `开发流程/04-检索与编排设计.md` §3.1 的 TicketState，
整体由 Checkpointer 持久化，以 session_id + ticket_id 作为恢复键。
"""

from typing import Any, TypedDict

from app.schemas.retrieval import Chunk
from app.schemas.ticket import Message


class TicketState(TypedDict, total=False):
    """图状态：节点间传递的唯一数据载体。"""

    # ---- 输入 ----
    session_id: str
    ticket_id: str
    query: str
    history: list[Message]

    # ---- 中间产物 ----
    intent: str
    need_review: bool
    retrieved: list[Chunk]
    retrieval_debug: dict[str, Any]
    draft: str
    citations: list[str]

    # ---- 审核与发送 ----
    review_status: str
    reviewer_comment: str
    reply: str
    send_status: str
    error: str
