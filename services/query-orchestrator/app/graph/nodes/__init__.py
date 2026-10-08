"""图节点工厂集合。

每个工厂接收「依赖 + 配置」并返回 ``async (state) -> partial_state``，
节点本身不持有全局状态，便于在测试里替换依赖。
"""

from app.graph.nodes.cache import make_cache_lookup_node, make_cache_store_node
from app.graph.nodes.generate import make_generate_node
from app.graph.nodes.guard import (
    build_citations,
    detect_refusal,
    extract_citation_indexes,
    make_guard_node,
    make_refuse_node,
)
from app.graph.nodes.plan import make_plan_node
from app.graph.nodes.rerank import make_rerank_node
from app.graph.nodes.retrieve import make_retrieve_node
from app.graph.nodes.rewrite import make_rewrite_node
from app.graph.nodes.route import make_route_node

__all__ = [
    "build_citations",
    "detect_refusal",
    "extract_citation_indexes",
    "make_cache_lookup_node",
    "make_cache_store_node",
    "make_generate_node",
    "make_guard_node",
    "make_plan_node",
    "make_refuse_node",
    "make_rerank_node",
    "make_retrieve_node",
    "make_rewrite_node",
    "make_route_node",
]
