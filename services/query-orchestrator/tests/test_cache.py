"""查询缓存键的回归测试。

这里每一条断言都对应一个真实的越权或串味场景，不是形式化检查。
"""

from __future__ import annotations

import hashlib

from app.clients.cache import QueryCache


def _key(
    tenant: str = "default",
    department: str = "hr",
    user: str = "alice",
    mode: str = "hybrid",
    top_k: int = 5,
    query: str = "招聘需求审批中，编制核对需要在几个工作日内完成？",
    temperature: float | None = None,
    version: str = "1",
    model: str | None = None,
) -> str:
    return QueryCache._key(
        tenant, department, user, mode, top_k, query, temperature, version, model
    )


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


def test_cache_key_separates_models() -> None:
    """模型是逐请求可选的，答案必然不同，必须分键。

    否则「切到新模型 → 继续命中旧模型缓存」，症状是明明选了新模型，
    答案还是老模型的口吻/事实，且要等 TTL 过期才恢复。
    """
    assert _key(model="qwen-plus") != _key(model="deepseek-chat")


def test_cache_key_model_none_keeps_legacy_shape() -> None:
    """model 留空（用网关默认模型）时，键与引入该参数**之前**的实现逐字节一致。

    用旧算法（8 个分量、不含 model）独立算出期望值再比对——否则上线即全量回源，
    存量缓存全部作废。model=None 时必须不追加任何分量。
    """
    legacy_parts = "|".join(
        [
            "1",
            "default",
            "hr",
            "alice",
            "hybrid",
            "5",
            "None",
            "招聘需求审批中，编制核对需要在几个工作日内完成？",
        ]
    )
    legacy_digest = hashlib.sha256(legacy_parts.encode()).hexdigest()
    assert _key() == f"rag:cache:v1:{legacy_digest}"


def test_cache_key_model_given_appends_distinct_key() -> None:
    """显式选模型时必须与默认模型分键，且键内含该模型名。"""
    picked = _key(model="qwen-plus")
    assert picked != _key()
    assert picked != _key(model="deepseek-chat")


def test_cache_key_separates_versions() -> None:
    """版本号是**显式失效开关**：换作答模型后必须让旧答案立刻失配。

    编排层不知道下游实际用哪个模型，所以无法把模型名拼进键；
    靠 CACHE_VERSION 让"换模型/改提示词/改切分"这些变化能被主动作废。
    没有这条，切到 DeepSeek 后会继续命中 4B 的旧答案直到 TTL 过期。
    """
    assert _key(version="1") != _key(version="2")


def test_cache_key_includes_version_in_prefix() -> None:
    """前缀里带版本，便于运维按版本批量清理（KEYS rag:cache:v1:*）。"""
    assert _key(version="7").startswith("rag:cache:v7:")
