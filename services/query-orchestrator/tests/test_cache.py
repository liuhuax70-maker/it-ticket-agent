"""查询缓存键的回归测试。

这里每一条断言都对应一个真实的越权或串味场景，不是形式化检查。
"""

from __future__ import annotations

from app.clients.cache import QueryCache


def _key(
    tenant: str = "default",
    department: str = "hr",
    user: str = "alice",
    mode: str = "hybrid",
    top_k: int = 5,
    query: str = "招聘需求审批中，编制核对需要在几个工作日内完成？",
    temperature: float | None = None,
) -> str:
    return QueryCache._key(tenant, department, user, mode, top_k, query, temperature)


def test_cache_key_separates_departments() -> None:
    """跨部门串味 = 越权泄露：alice 的答案带 HR 文档引用，bob 不该收到。"""
    assert _key(department="hr") != _key(department="engineering")


def test_cache_key_separates_users_within_department() -> None:
    """private 可见性按 owner 过滤，所以同部门不同人也必须分开。"""
    assert _key(user="alice") != _key(user="bob")


def test_cache_key_separates_tenants() -> None:
    assert _key(tenant="default") != _key(tenant="tenant-b")


def test_cache_key_separates_retrieval_shape() -> None:
    assert _key(mode="hybrid") != _key(mode="vector")
    assert _key(top_k=5) != _key(top_k=8)


def test_cache_key_separates_temperature() -> None:
    """评测用 temperature=0 换取可复现；与默认温度的结果不能共用一条缓存。"""
    assert _key(temperature=None) != _key(temperature=0.0)
    assert _key(temperature=0.0) != _key(temperature=0.7)


def test_cache_key_normalizes_query_whitespace() -> None:
    """空白差异不该造成穿透。"""
    assert _key(query="年假有多少天？") == _key(query="  年假有多少天？  ")


def test_cache_key_handles_missing_identity_parts() -> None:
    """匿名请求（占位身份）也要能算出稳定的键，不能因为 None 抛异常。"""
    assert _key(department="", user="") != ""
    assert _key(department="", user="") == _key(department="", user="")
