"""过滤契约编译（retrieval 服务侧接缝）。

真正的语义在 ``packages.retrievers.filters.compile_filters``（唯一实现），
本模块只做两件事：
    1. 把上游传来的 ``ACL`` 转成 ``FilterDict`` 契约；
    2. 支持请求级 ``doc_ids`` 白名单等检索特有扩展。
"""

from __future__ import annotations

from typing import Any

from packages.contracts import ACL
from packages.retrievers import compile_filters
from packages.retrievers.base import FilterDict


def build_filters(acl: ACL | None, doc_ids: list[str] | None = None) -> FilterDict | None:
    """把上游 ACL + 可选 doc_ids 白名单编译为过滤契约。

    缺 ACL 时返回 ``None``——这是**有意的**：由 ``RetrievalService.search`` 决定走
    fail-closed 拒绝还是放行（安全开关），而不是在这里偷偷放行。
    """
    return compile_filters(acl, doc_ids)


def extract_doc_ids(extra: dict[str, Any] | None) -> list[str] | None:
    """从 SearchRequest.filters 里取文档白名单（可选能力）。"""
    if not extra:
        return None
    raw = extra.get("doc_ids")
    if not raw:
        return None
    return [str(x) for x in raw]
