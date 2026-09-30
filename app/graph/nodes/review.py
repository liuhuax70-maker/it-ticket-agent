"""人工审核节点：以 LangGraph interrupt 挂起，等待审核结果后恢复。"""

from app.graph.state import TicketState


def review_node(state: TicketState) -> dict:
    """调用 interrupt() 挂起图执行，审核结果经 Command(resume=...) 注入。

    跳转：approved → finalize_node；rejected → END。
    """
    # TODO(后续)：使用 langgraph.types.interrupt 挂起
    raise NotImplementedError("骨架占位：review_node 将在后续编码阶段实现")
