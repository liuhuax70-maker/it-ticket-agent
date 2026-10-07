"""RAG 图状态定义。

约定：
    * 状态只放**可序列化的业务数据**，不放客户端对象（客户端由节点闭包持有）；
    * ``contexts`` 的顺序 = 引用编号顺序，citations 必须由同一列表派生，
      否则会出现「引用张冠李戴」。
"""

from __future__ import annotations

from typing import TypedDict

from packages.contracts import ACL, Citation, ContextItem, RetrieveMode, SearchHit


class RAGState(TypedDict, total=False):
    # ---- 输入 ----
    query: str
    tenant_id: str
    department_id: str
    user_id: str
    roles: list[str]
    top_k: int
    mode: RetrieveMode
    temperature: float | None
    trace_id: str
    # 本次请求是否允许读写查询缓存。由 ChatRequest.use_cache 决定，
    # 缓存节点据此整体跳过——评测传 False，否则缓存命中会改变可测量的指标
    # （命中响应没有 contexts，检索侧指标的样本会被悄悄剔除）。
    use_cache: bool

    # ---- 中间态 ----
    rewritten_query: str
    # ⚠️ ACL 必须由 route 节点产出（它从网关下传的身份构造），初始 state 里刻意不给。
    # 检索节点用 ``state.get("acl")``：一旦有人把 route 从图里摘掉，ACL 会静默变成
    # None，而 None 在 filters.compile_filters 里表示"放弃全部过滤" = 全库召回。
    acl: ACL | None
    hits: list[SearchHit]
    contexts: list[ContextItem]

    # ---- 输出 ----
    answer: str
    citations: list[Citation]
    refused: bool
    cached: bool
    model: str | None
    reranker: str
    timings: dict[str, float]
    errors: list[str]
