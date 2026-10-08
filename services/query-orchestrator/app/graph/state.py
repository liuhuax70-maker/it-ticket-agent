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
    """RAG 图可变状态（全部可序列化业务数据，不放客户端对象）。

    关键不变量：``contexts`` 的顺序 = 引用编号顺序，citations 必须由同一列表派生，
    否则出现「引用张冠李戴」。``acl`` 必须由 route 节点产出，初始刻意不给。
    """

    # ---- 输入 ----
    query: str
    tenant_id: str
    department_id: str
    user_id: str
    roles: list[str]
    top_k: int
    mode: RetrieveMode
    temperature: float | None
    # 请求方指定的生成模型（ChatRequest.model）。留空由 model-gateway 决定。
    # ⚠️ 同一个 key 兼作输出：generate 节点会用"实际生效的模型"覆盖它，
    # 这样 ChatResponse.model 报的一定是真正作答的那个模型，而不是请求的意图。
    model: str | None
    trace_id: str
    # 本次请求是否允许读写查询缓存。由 ChatRequest.use_cache 决定，
    # 缓存节点据此整体跳过——评测传 False，否则缓存命中会改变可测量的指标
    # （命中响应没有 contexts，检索侧指标的样本会被悄悄剔除）。
    use_cache: bool

    # ---- 中间态 ----
    rewritten_query: str
    # 规划节点产出：可独立检索的子查询列表（单信息点问题就是 [原查询]）。
    # retrieve 节点对每个子查询独立检索，再用 RRF 融合——多跳问题的
    # 每类信息点都能以自己的关键词参赛，而不是挤在一条查询里互相压制。
    sub_queries: list[str]
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
    # model 见输入段：输入=请求的模型，输出=实际作答的模型（同一 key 复用）
    reranker: str
    timings: dict[str, float]
    errors: list[str]
