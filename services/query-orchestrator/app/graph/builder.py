"""构建 LangGraph 图。

拓扑（最小闭环）：
    START -> cache_lookup -命中-> END
                         -未命中-> rewrite -> route -> retrieve -空-> refuse -> END
                                                          -非空-> rerank -> generate
                                                                        -> guard -> cache_store -> END

每个节点只做一件事，``edges.py`` 负责分支判断；
加「多轮 / 反思 / GraphRAG」只需在图上挂新节点，不必改动已有节点。
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.clients.cache import QueryCache
from app.clients.model_gateway import ModelGatewayClient
from app.clients.retrieval import RetrievalClient
from app.config import Settings
from app.graph.edges import (
    NODE_CACHE_LOOKUP,
    NODE_CACHE_STORE,
    NODE_GENERATE,
    NODE_GUARD,
    NODE_REFUSE,
    NODE_RERANK,
    NODE_RETRIEVE,
    NODE_REWRITE,
    NODE_ROUTE,
    after_cache_lookup,
    after_generate,
    after_retrieve,
)
from app.graph.nodes import (
    make_cache_lookup_node,
    make_cache_store_node,
    make_generate_node,
    make_guard_node,
    make_refuse_node,
    make_rerank_node,
    make_retrieve_node,
    make_rewrite_node,
    make_route_node,
)
from app.graph.state import RAGState
from packages.common.logging import get_logger

logger = get_logger("orchestrator.builder")


def build_graph(
    *,
    retrieval: RetrievalClient,
    model_gateway: ModelGatewayClient,
    cache: QueryCache,
    settings: Settings,
):
    graph = StateGraph(RAGState)

    graph.add_node(NODE_CACHE_LOOKUP, make_cache_lookup_node(cache, settings))
    graph.add_node(NODE_REWRITE, make_rewrite_node(model_gateway, settings))
    graph.add_node(NODE_ROUTE, make_route_node(settings))
    graph.add_node(NODE_RETRIEVE, make_retrieve_node(retrieval, settings))
    graph.add_node(NODE_RERANK, make_rerank_node(retrieval, settings))
    graph.add_node(NODE_GENERATE, make_generate_node(model_gateway, settings))
    graph.add_node(NODE_GUARD, make_guard_node(model_gateway, settings))
    graph.add_node(NODE_REFUSE, make_refuse_node(settings))
    graph.add_node(NODE_CACHE_STORE, make_cache_store_node(cache, settings))

    graph.add_edge(START, NODE_CACHE_LOOKUP)
    graph.add_conditional_edges(
        NODE_CACHE_LOOKUP, after_cache_lookup, {NODE_REWRITE: NODE_REWRITE, END: END}
    )
    graph.add_edge(NODE_REWRITE, NODE_ROUTE)
    graph.add_edge(NODE_ROUTE, NODE_RETRIEVE)
    graph.add_conditional_edges(
        NODE_RETRIEVE, after_retrieve, {NODE_RERANK: NODE_RERANK, NODE_REFUSE: NODE_REFUSE}
    )
    graph.add_edge(NODE_RERANK, NODE_GENERATE)
    graph.add_conditional_edges(
        NODE_GENERATE, after_generate, {NODE_GUARD: NODE_GUARD, NODE_REFUSE: NODE_REFUSE}
    )
    graph.add_edge(NODE_GUARD, NODE_CACHE_STORE)
    graph.add_edge(NODE_CACHE_STORE, END)
    graph.add_edge(NODE_REFUSE, END)

    logger.debug("RAG 图构建完成: %s 个节点", len(graph.nodes))
    return graph.compile()
