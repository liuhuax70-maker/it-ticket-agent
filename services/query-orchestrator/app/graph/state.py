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
    trace_id: str

    # ---- 中间态 ----
    rewritten_query: str
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
