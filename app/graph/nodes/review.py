"""人工审核节点：以 LangGraph `interrupt` 挂起，等待审核结果后恢复。

- 挂起时状态由 Checkpointer 持久化；
- 恢复时通过 `Command(resume=<审核载荷>)` 注入，`interrupt()` 的返回值即该载荷；
- 载荷格式见 `开发流程/05-接口与数据契约设计.md` §2.5。

跳转：approved → finalize；rejected → END（不发送）。
"""

from langgraph.types import interrupt

from app.core.logging import get_logger
from app.graph.state import TicketState
from app.schemas.ticket import Intent, ReviewStatus

logger = get_logger(__name__)


def review_node(state: TicketState) -> dict:
    """挂起等待人工审核，返回审核结论。"""
    reason = (
        "命中敏感关键词"
        if state.get("intent") == Intent.SENSITIVE.value
        else "配置要求全部工单人工审核"
    )

    payload = {
        "ticket_id": state.get("ticket_id"),
        "session_id": state.get("session_id"),
        "draft": state.get("draft", ""),
        "citations": state.get("citations") or [],
        "reason": reason,
    }

    # 挂起：此处返回的即 resume 注入的载荷
    decision = interrupt(payload)

    if not isinstance(decision, dict):
        logger.warning("审核载荷格式异常（%r），按驳回处理", decision)
        decision = {}

    status = decision.get("decision", ReviewStatus.REJECTED.value)
    if status not in (ReviewStatus.APPROVED.value, ReviewStatus.REJECTED.value):
        logger.warning("未知审核结论 %r，按驳回处理", status)
        status = ReviewStatus.REJECTED.value

    logger.info("收到人工审核结论: ticket_id=%s decision=%s", state.get("ticket_id"), status)

    return {
        "review_status": status,
        "reviewer_comment": decision.get("comment") or "",
        "edited_reply": decision.get("edited_reply"),
    }
