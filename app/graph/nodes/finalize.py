"""定稿节点：产出最终回复文本。

两种来源：
- 普通工单：直接使用 draft；
- 审核通过：优先使用人工修改后的 edited_reply。
"""

from app.graph.state import TicketState


def finalize_node(state: TicketState) -> dict:
    """返回 reply。"""
    # TODO(后续)：合并 draft / edited_reply，生成最终 reply
    raise NotImplementedError("骨架占位：finalize_node 将在后续编码阶段实现")
