"""图装配：节点注册 + 条件路由 + Checkpointer 绑定。

拓扑：

    START → intent → retrieve ─┬─(有命中)─► draft ─┬─(需审核)─► review ─┬─(approved)─► finalize
                               │                  │                   └─(rejected)──► END
                               │                  └─(无需审核)──────────────────────► finalize
                               └─(无命中)───────────────────────────────────────────► finalize
                                                                            finalize → send → END

> 说明：`intent → retrieve` 是**无条件**的。无论是否敏感都必须检索，否则草稿没有依据；
> 敏感与否只影响 `draft` 之后是否插入 `review` 节点。
> （`开发流程/04` §3.3 的示意图在这一点上不够清晰，已按此处实现并以本文件为准。）
"""

from typing import Any

from langgraph.graph import END, START, StateGraph

from app.core.logging import get_logger
from app.graph.nodes.draft import draft_node
from app.graph.nodes.finalize import finalize_node
from app.graph.nodes.intent import intent_node
from app.graph.nodes.retrieve import retrieve_node
from app.graph.nodes.review import review_node
from app.graph.nodes.send import send_node
from app.graph.state import TicketState
from app.memory.checkpointer import get_checkpointer
from app.schemas.ticket import ReviewStatus

logger = get_logger(__name__)


def route_after_retrieve(state: TicketState) -> str:
    """有命中才生成草稿；无命中直接走定稿（「未找到」回复）。"""
    return "draft" if state.get("retrieved") else "finalize"


def route_after_draft(state: TicketState) -> str:
    """敏感 / 配置要求全部审核 → review；否则直接定稿。"""
    return "review" if state.get("need_review") else "finalize"


def route_after_review(state: TicketState) -> str:
    """审核通过才继续；驳回直接结束（不发送）。"""
    if state.get("review_status") == ReviewStatus.APPROVED.value:
        return "finalize"
    return "end"


def build_graph(checkpointer=None):
    """构建并编译 LangGraph 图。"""
    graph = StateGraph(TicketState)

    graph.add_node("intent", intent_node)
    graph.add_node("retrieve", retrieve_node)
    graph.add_node("draft", draft_node)
    graph.add_node("review", review_node)
    graph.add_node("finalize", finalize_node)
    graph.add_node("send", send_node)

    graph.add_edge(START, "intent")
    graph.add_edge("intent", "retrieve")
    graph.add_conditional_edges(
        "retrieve", route_after_retrieve, {"draft": "draft", "finalize": "finalize"}
    )
    graph.add_conditional_edges(
        "draft", route_after_draft, {"review": "review", "finalize": "finalize"}
    )
    graph.add_conditional_edges(
        "review", route_after_review, {"finalize": "finalize", "end": END}
    )
    graph.add_edge("finalize", "send")
    graph.add_edge("send", END)

    return graph.compile(checkpointer=checkpointer or get_checkpointer())


_graph: Any = None


async def get_graph():
    """编译后的图单例（首次调用时异步初始化 Checkpointer）。"""
    global _graph

    if _graph is None:
        checkpointer = await get_checkpointer()
        _graph = build_graph(checkpointer=checkpointer)
        logger.info("编译 LangGraph 编排图")

    return _graph


def reset_graph() -> None:
    """清空图单例（测试或切换 Checkpointer 时使用）。"""
    global _graph
    _graph = None
