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

    ``acl is None`` 表示调用方**显式放弃全部权限约束**，只在内部调试时可用。

    ⚠️ 这个"放弃"是彻底的，不是"宽松一点"：``None`` / 空契约会被两个 store
    分别翻译成 ``filter=[]``（OpenSearch）与 ``expr=""``（Milvus），
    而"空过滤条件"在两个引擎里都意味着 **match-all**——也就是不分租户、
    不分部门的全库召回，且不会报任何错。
    因此：

        * 生产链路上 ``acl`` 必须由 :meth:`Identity.to_acl` 从网关下传的身份构造，
          缺失就应该 fail-closed（报错），而不是悄悄退化成全库检索；
        * ``services/retrieval`` 收到 ``acl is None`` 时只打 warning 并继续，
          属于**未受控降级**——检索端口可被直连，这条 warning 是唯一的告警信号，
          不要在重构里把它删掉或降级为 debug。
    """
    if acl is None:
        return {"doc_ids": list(doc_ids)} if doc_ids else None

    clauses: list[dict[str, Any]] = [{"visibility": "public"}, {"visibility": "internal"}]
    if acl.department_id:
        clauses.append({"visibility": "department", "department_id": acl.department_id})
    # owner 缺失时**整条 private 分支被丢弃**（而不是放宽成"所有人可见 private"）。
    # 方向是 fail-closed：宁可让人看不到自己的私有文档，也不要让别人看到。
    # 代价是"owner 为空 + visibility=private"的文档谁都检索不到——
    # 这类脏数据由接入侧拦截（ingestion 对 private 缺 owner 直接 422）。
    if acl.owner:
        clauses.append({"visibility": "private", "owner": acl.owner})

    filters: FilterDict = {
        "must": {"tenant_id": acl.tenant_id},
        "visibility_clauses": clauses,
    }
    if doc_ids:
        filters["doc_ids"] = list(doc_ids)
    return filters
