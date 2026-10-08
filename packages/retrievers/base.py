"""检索器抽象与过滤契约（``FilterDict``）。

``FilterDict`` 是 stores 与业务层之间的唯一约定，store 只做机械翻译（Milvus
``_compile_expr`` / OpenSearch ``_compile_filter``）。编译发生在编排层 route 节点
（由 Identity 构造 ACL 后调用 :func:`~packages.retrievers.filters.compile_filters`），
``services/retrieval/app/filters.py`` 只是薄转发，不解析身份。

语义：clause 之间 OR、clause 内部 AND，``must`` 与所有 clause AND；``must_not`` 逐条排除。
``must.tenant_id`` 对**所有**分支生效——``public`` 也限本租户。

过滤必须下沉到 store，不允许在应用层裁剪 top_k：那会让越权文档挤占 top_k，把有权限的
文档挤出去。

``must_not`` 用排除而非 ``must: {lifecycle: active}``：存量文档没有该字段，两种引擎都
天然通过排除，零迁移；写成 ``must`` 会让上线即全库搜不到。
"""

from __future__ import annotations

from typing import Any, Protocol, TypedDict

from packages.contracts import SearchHit


class FilterDict(TypedDict, total=False):
    """检索过滤契约：stores 与业务层之间的唯一约定，两个 store 必须 1:1 翻译。

    ``must`` 全等、``visibility_clauses`` 任一命中、``doc_ids`` 限定文档、
    ``must_not`` 排除——语义详见模块顶部注释（clause 间 OR、clause 内 AND、
    ``must`` 对所有分支生效、``must_not`` 字段缺失视为通过）。
    """

    must: dict[str, Any]
    visibility_clauses: list[dict[str, Any]]
    doc_ids: list[str]
    must_not: list[dict[str, Any]]


class Retriever(Protocol):
    """检索器协议：任何 store 后端实现 ``retrieve``（带过滤）与 ``aclose`` 即可接入编排层。"""

    name: str

    async def retrieve(
        self, query: str, top_k: int, filters: FilterDict | None = None
    ) -> list[SearchHit]: ...

    async def aclose(self) -> None: ...


RETRIEVER_VECTOR = "vector"
RETRIEVER_BM25 = "bm25"
RETRIEVER_HYBRID = "hybrid"
