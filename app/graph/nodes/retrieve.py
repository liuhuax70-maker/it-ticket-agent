"""检索节点：封装混合检索（稠密 + BM25 → RRF → 可选重排）。

跳转：有命中 → draft；无命中 → finalize（走「未找到」回复）。
"""

from app.core.logging import get_logger
from app.graph.state import TicketState
from app.retrieval.hybrid import hybrid_search

logger = get_logger(__name__)


def retrieve_node(state: TicketState) -> dict:
    """执行混合检索，把片段转成普通 dict 存入状态（便于序列化与观测）。"""
    query = state.get("query", "")

    chunks, mode, debug = hybrid_search(query)

    if not chunks:
        logger.warning(
            "检索无命中: ticket_id=%s mode=%s dense_error=%s sparse_error=%s",
            state.get("ticket_id"),
            mode.value,
            debug.get("dense_error"),
            debug.get("sparse_error"),
        )

    return {
        # mode="json"：把枚举等自定义类型转成原生 JSON 值，
        # 否则 checkpointer 反序列化时会报「unregistered type」警告
        "retrieved": [chunk.model_dump(mode="json") for chunk in chunks],
        "retrieval_debug": debug,
    }
