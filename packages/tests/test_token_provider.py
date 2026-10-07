"""令牌换取与缓存：两个容易被忽视的行为必须有测试。

1. **提前量**（refresh_slack）：剩余时间不足 30s 就视为不可用，
   否则请求发出时刚好过期，表现为"偶发 401"。
2. **进程级一次性熔断**：首次失败后不再尝试。这条是刻意的（避免 Keycloak
   不可达时每条样本都重试把整轮拖死），但后果很重——降级后权限类样本
   全部失去验证意义，所以必须被测试钉住，避免有人"顺手"把它改成静默重试。
"""

from __future__ import annotations

import time

import httpx
import pytest

from packages.security.config import SecuritySettings
from packages.security.tokens import TokenProvider, _Cached


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def _install_httpx(monkeypatch, *, payload=None, error: Exception | None = None):
    """替换 httpx.AsyncClient，记录每次请求的表单，便于断言实际发出的内容。"""
    calls: list[dict] = []

    class _FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def post(self, url, data=None):
            calls.append({"url": url, "data": data or {}})
            if error is not None:
                raise error
            return _FakeResponse(payload or {})

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    return calls


def _settings(**overrides) -> SecuritySettings:
    base: dict = {"keycloak_client_id": "rag-api", "keycloak_client_secret": ""}
    base.update(overrides)
    return SecuritySettings(**base)


# ---------------- 缓存与提前量 ----------------


def test_cached_is_empty_initially() -> None:
    assert TokenProvider(_settings()).cached("alice") is None


def test_cached_returns_valid_token() -> None:
    provider = TokenProvider(_settings())
    provider._cache["alice"] = _Cached(access_token="tk", expires_at=time.time() + 300)
    assert provider.cached("alice") == "tk"


def test_cached_treats_token_inside_slack_as_expired() -> None:
    """剩余 10s、提前量 30s —— 必须视为不可用，否则就会发出一个即将过期的令牌。"""
    provider = TokenProvider(_settings(), refresh_slack=30.0)
    provider._cache["alice"] = _Cached(access_token="tk", expires_at=time.time() + 10)
    assert provider.cached("alice") is None


def test_cached_is_username_scoped() -> None:
    provider = TokenProvider(_settings())
    provider._cache["alice"] = _Cached(access_token="alice-tk", expires_at=time.time() + 300)
    assert provider.cached("bob") is None


# ---------------- 换取与缓存 ----------------


async def test_token_is_fetched_then_served_from_cache(monkeypatch) -> None:
    calls = _install_httpx(monkeypatch, payload={"access_token": "tk-1", "expires_in": 300})
    provider = TokenProvider(_settings())
    assert await provider.token("alice") == "tk-1"
    assert await provider.token("alice") == "tk-1"
    assert len(calls) == 1, "第二次必须命中缓存，而不是再换一次"


async def test_password_defaults_to_username(monkeypatch) -> None:
    """开发账号约定「口令 == 用户名」，这条约定写坏了会让所有脚本静默取不到令牌。"""
    calls = _install_httpx(monkeypatch, payload={"access_token": "tk", "expires_in": 300})
    await TokenProvider(_settings()).token("alice")
    assert calls[0]["data"]["password"] == "alice"
    assert calls[0]["data"]["username"] == "alice"
    assert calls[0]["data"]["grant_type"] == "password"


async def test_passwords_map_and_explicit_password_take_priority(monkeypatch) -> None:
    calls = _install_httpx(monkeypatch, payload={"access_token": "tk", "expires_in": 300})
    provider = TokenProvider(_settings(), passwords={"alice": "from-map"})
    await provider.token("alice")
    assert calls[0]["data"]["password"] == "from-map"
    await provider.token("bob", "explicit")
    assert calls[1]["data"]["password"] == "explicit"


async def test_client_secret_only_sent_when_configured(monkeypatch) -> None:
    calls = _install_httpx(monkeypatch, payload={"access_token": "tk", "expires_in": 300})
    await TokenProvider(_settings(keycloak_client_secret="")).token("alice")
    assert "client_secret" not in calls[0]["data"]
    await TokenProvider(_settings(keycloak_client_secret="s3cret")).token("alice")
    assert calls[1]["data"]["client_secret"] == "s3cret"


# ---------------- 失败与熔断 ----------------


async def test_failure_returns_none(monkeypatch) -> None:
    _install_httpx(monkeypatch, error=httpx.ConnectError("refused"))
    assert await TokenProvider(_settings()).token("alice") is None


async def test_failure_trips_breaker_and_stops_trying(monkeypatch) -> None:
    """熔断后不再发请求。改这条行为前请读模块 docstring 的代价说明。"""
    calls = _install_httpx(monkeypatch, error=httpx.ConnectError("refused"))
    provider = TokenProvider(_settings())
    assert await provider.token("alice") is None
    assert await provider.token("alice") is None
    assert await provider.token("bob") is None
    assert len(calls) == 1, "熔断后必须一次请求都不发"
    assert provider._unavailable is True


async def test_success_does_not_trip_breaker(monkeypatch) -> None:
    _install_httpx(monkeypatch, payload={"access_token": "tk", "expires_in": 300})
    provider = TokenProvider(_settings())
    await provider.token("alice")
    assert provider._unavailable is False


async def test_missing_access_token_in_response_raises(monkeypatch) -> None:
    """响应里没有 access_token 属于契约破坏，应当抛错而不是返回 None——
    返回 None 会被上层当成"Keycloak 不可用"从而误判降级原因。"""
    _install_httpx(monkeypatch, payload={"expires_in": 300})
    with pytest.raises(KeyError):
        await TokenProvider(_settings()).token("alice")


async def test_zero_expires_in_is_cached_but_immediately_stale(monkeypatch) -> None:
    """expires_in=0 时不应被当成"长期有效"：下一次调用必须重新换取。"""
    calls = _install_httpx(monkeypatch, payload={"access_token": "tk", "expires_in": 0})
    provider = TokenProvider(_settings())
    await provider.token("alice")
    await provider.token("alice")
    assert len(calls) == 2
