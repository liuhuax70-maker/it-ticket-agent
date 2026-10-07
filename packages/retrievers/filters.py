"""ACL -> 过滤契约的唯一编译器。

只此一处实现，被 retrieval 服务与任何持有 Identity 的服务复用，
避免「两个地方各写一套可见性语义」导致权限行为不一致。

可见性语义（``must.tenant_id`` 对所有分支生效，即 public 亦限本租户）：
    public      -> 租户内所有可见
    internal    -> 租户内所有可见
    department  -> 同部门可见
    private     -> 仅 owner 可见
"""

from __future__ import annotations

from typing import Any

from packages.contracts import ACL
from packages.retrievers.base import FilterDict


def compile_filters(acl: ACL | None, doc_ids: list[str] | None = None) -> FilterDict | None:
    """把请求方 ACL 编译成存储层可机械翻译的过滤契约。

    ``acl is None`` 表示调用方显式放弃权限约束（仅限内部调试），
    此时只应用 doc_ids 白名单。
    """
    if acl is None:
        return {"doc_ids": list(doc_ids)} if doc_ids else None

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
