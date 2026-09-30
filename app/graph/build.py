"""图装配：节点注册 + 条件边 + Checkpointer 绑定。

拓扑（对应 `开发流程/04-检索与编排设计.md` §3.3）：

    START → intent ─┬─(need_review)─► draft → review ─┬─(approved)─► finalize → send → END
                    └─(普通)────────► retrieve ─┬─(有命中)─► draft
                                                └─(无命中)─► finalize
"""

from app.graph.state import TicketState


def build_graph(checkpointer=None):
    """构建并编译 LangGraph 图。

    :param checkpointer: 状态持久化后端（默认从 app.memory.checkpointer 获取）
    """
    # TODO(后续)：使用 langgraph.graph.StateGraph 注册节点与条件边
    raise NotImplementedError("骨架占位：build_graph 将在后续编码阶段实现")
