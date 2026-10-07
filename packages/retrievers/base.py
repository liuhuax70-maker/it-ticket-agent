"""检索器抽象与共享过滤契约。

过滤契约（``FilterDict``）是 stores 与业务层之间的唯一约定，
由 ``services/retrieval/app/filters.py`` 从身份编译而来，stores 只做机械翻译：

    {
        "must":               {"tenant_id": "default"},          # 必须全等
        "visibility_clauses": [                                  # 任一命中即可
            {"visibility": "public"},
            {"visibility": "internal"},
            {"visibility": "department", "department_id": "hr"},
            {"visibility": "private", "owner": "u_1"},
        ],
        "doc_ids":            ["d_xxx"],                          # 可选，限定文档
    }

安全铁律：过滤必须**下沉**到 Milvus ``expr`` / OpenSearch ``filter``，
绝不允许在应用层对 top_k 结果做二次裁剪——那会让越权文档挤占 top_k，
导致有权限的文档检索不到（旧 P0 坑位 #9）。
"""

from __future__ import annotations

from typing import Any, Protocol, TypedDict

from packages.contracts import SearchHit


class FilterDict(TypedDict, total=False):
    must: dict[str, Any]
    visibility_clauses: list[dict[str, Any]]
    doc_ids: list[str]


class Retriever(Protocol):
    name: str

    async def retrieve(
        self, query: str, top_k: int, filters: FilterDict | None = None
    ) -> list[SearchHit]: ...


RETRIEVER_VECTOR = "vector"
RETRIEVER_BM25 = "bm25"
RETRIEVER_HYBRID = "hybrid"
