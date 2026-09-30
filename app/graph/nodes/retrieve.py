"""检索节点：封装混合检索（稠密 + BM25 → RRF → 可选重排）。"""

from app.graph.state import TicketState


def retrieve_node(state: TicketState) -> dict:
    """返回 retrieved（Top-K 片段）与 retrieval_debug（各路命中信息）。

    无命中时置空 retrieved，由条件边跳到 finalize 走「未找到」回复。
    """
    # TODO(后续)：调用 app.retrieval.hybrid.hybrid_search
    raise NotImplementedError("骨架占位：retrieve_node 将在后续编码阶段实现")
