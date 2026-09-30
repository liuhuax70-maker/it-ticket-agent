"""定稿节点：产出最终回复文本。

三种来源（优先级从高到低）：
1. 审核通过且人工修改过 → `edited_reply`
2. 审核通过 → `draft`
3. 未走审核（普通工单）→ `draft`

兜底：既无草稿也无审核结论（例如知识库无命中直接跳到本节点）→ `NOT_FOUND_REPLY`。
"""

from app.core.logging import get_logger
from app.generation.prompts import NOT_FOUND_REPLY
from app.graph.state import TicketState
from app.schemas.ticket import ReviewStatus

logger = get_logger(__name__)


def finalize_node(state: TicketState) -> dict:
    """合并草稿与人工修改，生成最终回复。"""
    status = state.get("review_status")

    if status == ReviewStatus.REJECTED.value:
        # 正常不会走到这里（驳回在路由层直接结束），保留防御
        logger.warning("已驳回的工单进入定稿节点: ticket_id=%s", state.get("ticket_id"))
        return {"reply": ""}

    if status == ReviewStatus.APPROVED.value:
        reply = state.get("edited_reply") or state.get("draft") or NOT_FOUND_REPLY
        if state.get("edited_reply"):
            logger.info("使用人工修改后的文案: ticket_id=%s", state.get("ticket_id"))
    else:
        reply = state.get("draft") or NOT_FOUND_REPLY

    return {"reply": reply}
