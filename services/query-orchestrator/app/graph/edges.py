"""图的节点名与条件跳转。

把「跳哪去」的判断集中在边函数里，节点只负责产出状态，
这样加节点时不需要回头读业务代码。
"""

from __future__ import annotations

from langgraph.graph import END

from app.graph.state import RAGState

# 节点名
NODE_CACHE_LOOKUP = "cache_lookup"
NODE_REWRITE = "rewrite"
NODE_PLAN = "plan"
NODE_ROUTE = "route"
NODE_RETRIEVE = "retrieve"
NODE_RERANK = "rerank"
NODE_GENERATE = "generate"
NODE_GUARD = "guard"
NODE_REFUSE = "refuse"
NODE_CACHE_STORE = "cache_store"


def after_cache_lookup(state: RAGState) -> str:
    """命中缓存直接结束；否则走改写。"""
    return END if state.get("cached") else NODE_REWRITE


def after_retrieve(state: RAGState) -> str:
    """检索为空 -> 拒答（不调用 LLM，从源头堵死幻觉）。"""
    if not state.get("hits"):
        return NODE_REFUSE
    return NODE_RERANK


def after_generate(state: RAGState) -> str:
    """生成结果为空（模型异常/被截断）时同样走拒答。"""
    if not (state.get("answer") or "").strip():
        return NODE_REFUSE
    return NODE_GUARD
