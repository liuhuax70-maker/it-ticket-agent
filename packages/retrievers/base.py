"""检索器抽象与共享过滤契约。

过滤契约（``FilterDict``）是 stores 与业务层之间的唯一约定，
stores 只做机械翻译（Milvus ``_compile_expr`` / OpenSearch ``_compile_filter``）。
**编译发生在编排层的 route 节点**（``services/query-orchestrator`` 里由 Identity
构造 ACL 后调用 :func:`packages.retrievers.filters.compile_filters`）；
``services/retrieval/app/filters.py`` 只是它的薄转发，不解析身份。

    {
        "must":               {"tenant_id": "default"},          # 必须全等
        "visibility_clauses": [                                  # 任一命中即可
            {"visibility": "public"},
            {"visibility": "internal"},
            {"visibility": "department", "department_id": "hr"},
            {"visibility": "private", "owner": "u_1"},
        ],
        "doc_ids":            ["d_xxx"],                          # 可选，限定文档
        "must_not":           [{"lifecycle": "retired"}],         # 必须**不**命中
    }

语义（两个 store 必须 1:1 翻译，改一处就要同步另一处）：
    clause 之间是 **OR**，clause 内部是 **AND**，``must`` 与所有 clause 是 **AND**；
    ``must_not`` 中的每条 clause 是**排除**（命中任一条即丢弃该条结果）。
    注意 ``must.tenant_id`` 对**所有**分支生效——即 ``public`` 也限本租户。

安全铁律：过滤必须**下沉**到 Milvus ``expr`` / OpenSearch ``filter``，
绝不允许在应用层对 top_k 结果做二次裁剪——那会让越权文档挤占 top_k，
导致有权限的文档检索不到（旧 P0 坑位 #9）。

``must_not`` 的取舍：字段缺失必须**视为通过**（而不是不匹配）。
    Milvus 未赋值的 VARCHAR 是 ``""``、OpenSearch 未写入的字段不参与 term 匹配，
    两种都天然满足"排除掉明确标了 retired 的"，所以不需要任何数据迁移。
    反过来若写成 ``must: {lifecycle: "active"}``，存量文档（没有这个字段）
    会被**整体过滤掉**——那是"上线即全库搜不到"的事故。
"""

from __future__ import annotations

from typing import Any, Protocol, TypedDict

from packages.contracts import SearchHit


class FilterDict(TypedDict, total=False):
    must: dict[str, Any]
    visibility_clauses: list[dict[str, Any]]
    doc_ids: list[str]
    must_not: list[dict[str, Any]]


class Retriever(Protocol):
    name: str

    async def retrieve(
        self, query: str, top_k: int, filters: FilterDict | None = None
    ) -> list[SearchHit]: ...


RETRIEVER_VECTOR = "vector"
RETRIEVER_BM25 = "bm25"
RETRIEVER_HYBRID = "hybrid"
