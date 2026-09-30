"""发送节点：发送最终回复并落状态。

幂等：若状态中已标记 `sent`，直接跳过，避免重复发送。
真实发送（对接工单系统 / 邮件 / IM）留待后续接入，此处只维护状态机。
"""

from app.core.logging import get_logger
from app.graph.state import TicketState
from app.schemas.ticket import SendStatus

logger = get_logger(__name__)


def send_node(state: TicketState) -> dict:
    """发送回复（幂等），返回发送状态。"""
    ticket_id = state.get("ticket_id")

    if state.get("send_status") == SendStatus.SENT.value:
        logger.info("工单已发送，跳过重复发送: ticket_id=%s", ticket_id)
        return {"send_status": SendStatus.SENT.value}

    reply = state.get("reply") or ""
    if not reply:
        logger.warning("回复为空，未发送: ticket_id=%s", ticket_id)
        return {"send_status": SendStatus.FAILED.value}

    # TODO(后续)：接入真实发送适配器（工单系统 API / 邮件 / IM）
    logger.info("发送回复: ticket_id=%s 长度=%d", ticket_id, len(reply))
    return {"send_status": SendStatus.SENT.value}
