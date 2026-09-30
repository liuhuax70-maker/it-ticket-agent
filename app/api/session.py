"""会话查询接口。

契约见 `开发流程/05-接口与数据契约设计.md` §4.3。
数据来源是 Checkpointer 的状态快照（`thread_id = session_id`）。
"""

from fastapi import APIRouter, Request

from app.core.errors import AppError, ErrorCode
from app.graph.build import get_graph
from app.memory.checkpointer import thread_config
from app.schemas.common import ApiResponse
from app.schemas.ticket import TicketStatus

router = APIRouter(prefix="/session", tags=["session"])


def _derive_status(values: dict, awaiting_review: bool) -> str:
    """由状态推导工单当前阶段。"""
    if awaiting_review:
        return TicketStatus.AWAITING_REVIEW.value
    if values.get("review_status") == "rejected":
        return TicketStatus.REJECTED.value
    if values.get("send_status") == "sent":
        return TicketStatus.SENT.value
    if values.get("send_status") == "failed":
        return TicketStatus.FAILED.value
    return TicketStatus.PROCESSING.value


@router.get("/{session_id}", response_model=ApiResponse[dict], summary="查询会话状态")
async def get_session(session_id: str, request: Request) -> ApiResponse[dict]:
    graph = await get_graph()
    config = thread_config(session_id)
    snapshot = await graph.aget_state(config)
    values = snapshot.values or {}

    if not values:
        raise AppError(ErrorCode.NOT_FOUND, f"会话不存在或已过期: {session_id}")

    awaiting_review = bool(snapshot.next)
    ticket = {
        "ticket_id": values.get("ticket_id"),
        "query": values.get("query"),
        "intent": values.get("intent"),
        "status": _derive_status(values, awaiting_review),
        "draft": values.get("draft"),
        "reply": values.get("reply"),
        "citations": values.get("citations") or [],
        "review_status": values.get("review_status"),
    }

    return ApiResponse(
        code=0,
        message="ok",
        trace_id=getattr(request.state, "trace_id", None),
        data={
            "session_id": session_id,
            "awaiting_review": awaiting_review,
            "tickets": [ticket],
        },
    )
