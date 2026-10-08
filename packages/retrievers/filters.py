"""ACL -> 过滤契约的唯一编译器。

只此一处实现，被 retrieval 与任何持有 Identity 的服务复用，避免两处各写一套可见性语义。
``must.tenant_id`` 对所有分支生效，因此 public 亦限本租户。
"""

from __future__ import annotations

from typing import Any

from packages.common.constants import EXCLUDE_LIFECYCLE
from packages.contracts import ACL
from packages.retrievers.base import FilterDict


def compile_filters(acl: ACL | None, doc_ids: list[str] | None = None) -> FilterDict | None:
    """把请求方 ACL 编译成存储层可机械翻译的过滤契约。

    ``acl is None`` 表示调用方**显式放弃全部权限约束**，只在内部调试时可用：``None``
    被两个 store 翻译成空过滤条件，而空过滤在两个引擎里都是 match-all——不分租户、
    不分部门的全库召回且不报错。所以生产链路必须由 ``Identity.to_acl`` 构造 acl，
    缺失应 fail-closed；retrieval 收到 ``None`` 时只 warning 继续，那条 warning 是唯一
    告警信号，不要在重构里删掉或降级为 debug。
    """
    if acl is None:
        return {"doc_ids": list(doc_ids)} if doc_ids else None

    clauses: list[dict[str, Any]] = [{"visibility": "public"}, {"visibility": "internal"}]
    if acl.department_id:
        clauses.append({"visibility": "department", "department_id": acl.department_id})
    # owner 缺失时整条 private 分支被丢弃，而不是放宽成"所有人可见 private"：
    # 方向是 fail-closed。代价是这类脏数据谁都检索不到，由接入侧拦截（缺 owner 直接 422）
    if acl.owner:
        clauses.append({"visibility": "private", "owner": acl.owner})

    filters: FilterDict = {
        "must": {"tenant_id": acl.tenant_id},
        "visibility_clauses": clauses,
        # 废止文档不参与检索，与租户隔离同级。放在编译器而非调用方，是为了让它不可能
        # 被忘记——漏传一次废止文档就会重新出现在答案里，而那不会报错。
        "must_not": EXCLUDE_LIFECYCLE,
    }
    if doc_ids:
        filters["doc_ids"] = list(doc_ids)
    return filters