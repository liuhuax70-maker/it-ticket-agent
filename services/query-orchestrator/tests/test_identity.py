"""编排层身份入口必须 fail-closed：请求头缺失就拒绝，绝不静默用默认租户。

回归动机：早期实现写成 ``headers.get("x-tenant-id") or settings.default_tenant_id``，
身份注入链路一断（直连本服务 / 反向代理丢 header），就会安静地"以默认租户身份检索"——
返回的是默认租户能看到的文档，日志里连一条 warning 都没有。
权限字段的错误不会抛异常，只会悄悄跨租户，所以这里必须响亮地失败。
"""

from __future__ import annotations

import pytest
from app.main import identity_from_headers
from starlette.requests import Request

from packages.common.errors import Forbidden


def _request(headers: dict[str, str]) -> Request:
    raw = [(key.lower().encode(), value.encode()) for key, value in headers.items()]
    return Request({"type": "http", "headers": raw, "method": "POST", "path": "/chat"})


_FULL = {
    "x-user-id": "u_alice",
    "x-tenant-id": "default",
    "x-department-id": "hr",
    "x-user-roles": "rag_reader, rag_admin",
}


def test_all_headers_present_gives_identity() -> None:
    identity = identity_from_headers(_request(_FULL))
    assert identity.user_id == "u_alice"
    assert identity.tenant_id == "default"
    assert identity.department_id == "hr"
    assert identity.roles == ["rag_reader", "rag_admin"]


@pytest.mark.parametrize("missing", ["x-user-id", "x-tenant-id", "x-department-id"])
def test_any_missing_header_is_rejected(missing: str) -> None:
    headers = {k: v for k, v in _FULL.items() if k != missing}
    with pytest.raises(Forbidden, match="缺少身份请求头"):
        identity_from_headers(_request(headers))


def test_no_headers_at_all_is_rejected_not_defaulted() -> None:
    """空头必须报错——这正是"静默降级成默认租户"的入口。"""
    with pytest.raises(Forbidden):
        identity_from_headers(_request({}))


def test_missing_roles_is_tolerated() -> None:
    """roles 缺失是合法的（普通用户没有特权角色），不能和身份缺失混为一谈。"""
    headers = {k: v for k, v in _FULL.items() if k != "x-user-roles"}
    identity = identity_from_headers(_request(headers))
    assert identity.roles == []
