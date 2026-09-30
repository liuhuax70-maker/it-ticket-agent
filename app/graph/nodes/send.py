"""发送节点：发送最终回复并落库（以 ticket_id 幂等）。"""

from app.graph.state import TicketState


def send_node(state: TicketState) -> dict:
    """返回 send_status（sent / failed）。

    幂等：同一 ticket_id 重复进入不重复发送。
    """
    # TODO(后续)：调用发送适配器 + 幂等校验
    raise NotImplementedError("骨架占位：send_node 将在后续编码阶段实现")
