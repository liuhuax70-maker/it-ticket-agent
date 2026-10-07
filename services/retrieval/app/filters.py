"""过滤契约编译（服务侧接缝）。

上游（api-gateway / query-orchestrator）只传 ``ACL``，本模块把它编译成
``FilterDict`` 契约，然后**原样下传**给 Milvus / OpenSearch。

安全铁律：过滤必须下沉到存储层。
若在这里对结果做二次裁剪，越权 chunk 会先挤占 top_k，导致有权限的文档检索不到。
"""

from __future__ import annotations

from typing import Any

from packages.contracts import ACL
from packages.retrievers.base import FilterDict


def build_filters(acl: ACL | None, doc_ids: list[str] | None = None) -> FilterDict | None:
    """ACL -> FilterDict。``acl`` 为 None 表示调用方放弃了权限约束（仅内部调试）。"""
    if acl is None:
        return {"doc_ids": doc_ids} if doc_ids else None

    clauses: list[dict[str, Any]] = [{"visibility": "public"}, {"visibility": "internal"}]
    if acl.department_id:
        clauses.append({"visibility": "department", "department_id": acl.department_id})
    if acl.owner:
        clauses.append({"visibility": "private", "owner": acl.owner})

    filters: FilterDict = {
        "must": {"tenant_id": acl.tenant_id},
        "visibility_clauses": clauses,
    }
    if doc_ids:
        filters["doc_ids"] = list(doc_ids)
    return filters


def extract_doc_ids(extra: dict[str, Any] | None) -> list[str] | None:
    """从 SearchRequest.filters 里取文档白名单（可选的能力）。"""
    if not extra:
        return None
    raw = extra.get("doc_ids")
    if not raw:
        return None
    return [str(x) for x in raw]
