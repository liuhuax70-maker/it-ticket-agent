"""OPA 决策的 fail-closed 契约。

授权判定是"错了就直接放行"的位置，而覆盖率显示这个模块之前基本没被测过（27%）。
不需要 OPA 实例：httpx 被替换成脚本化的假客户端。
"""

from __future__ import annotations

import httpx
import pytest

from packages.security.config import SecuritySettings
from packages.security.opa import OpaClient


class _FakeResponse:
    def __init__(self, payload=None, *, error: Exception | None = None) -> None:
        self._payload = payload
        self._error = error

    def raise_for_status(self) -> None:
        if self._error is not None:
            raise self._error

    def json(self):
        return self._payload


def _install_httpx(monkeypatch, *, payload=None, error: Exception | None = None):
    """替换 httpx.AsyncClient，并记录 (url, body) 便于断言请求目标。"""
    calls: list[tuple[str, dict]] = []

    class _FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc) -> bool:
            return False

        async def post(self, url, json=None):
            calls.append((url, json or {}))
            if error is not None:
                raise error
            return _FakeResponse(payload)

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    return calls


def _settings(**overrides) -> SecuritySettings:
    base: dict = {
        "authz_enabled": True,
        "opa_url": "http://opa:8181",
        "opa_decision_path": "v1/data/rag",
    }
    base.update(overrides)
    return SecuritySettings(**base)


async def test_disabled_client_allows_without_calling_opa(monkeypatch) -> None:
    """显式关闭时不发任何请求——否则等于"关了鉴权还要依赖 OPA 可用"。"""
    calls = _install_httpx(monkeypatch, payload={"result": False})
    client = OpaClient(_settings(), enabled=False)
    assert await client.allow({"action": "chat"}) == (True, "authz disabled")
    assert calls == []


async def test_boolean_true_result_allows(monkeypatch) -> None:
    _install_httpx(monkeypatch, payload={"result": True})
    assert await OpaClient(_settings()).allow({"action": "chat"}) == (True, "opa allow")


async def test_boolean_false_result_denies(monkeypatch) -> None:
    _install_httpx(monkeypatch, payload={"result": False})
    assert await OpaClient(_settings()).allow({"action": "chat"}) == (False, "opa deny")


async def test_object_result_carries_reason(monkeypatch) -> None:
    """查整个 package 而非单查 allow，就是为了拿到 reason——审计要能回答"为什么被拦"。"""
    _install_httpx(
        monkeypatch, payload={"result": {"allow": False, "reason": "缺少 documents:delete"}}
    )
    allowed, reason = await OpaClient(_settings()).allow({"action": "documents:delete"})
    assert allowed is False
    assert reason == "缺少 documents:delete"


async def test_object_result_without_allow_field_denies(monkeypatch) -> None:
    """result 是对象但没写 allow 时默认拒绝——不能因为"没写"就放行。"""
    _install_httpx(monkeypatch, payload={"result": {"reason": "policy 未定义该动作"}})
    allowed, _ = await OpaClient(_settings()).allow({"action": "unknown"})
    assert allowed is False


async def test_unreachable_opa_fails_closed(monkeypatch) -> None:
    """OPA 不可达必须拒绝。这条是整个模块的安全取向，改动它等于开后门。"""
    _install_httpx(monkeypatch, error=httpx.ConnectError("connection refused"))
    allowed, reason = await OpaClient(_settings()).allow({"action": "chat"})
    assert allowed is False
    assert "unavailable" in reason


async def test_http_error_status_fails_closed(monkeypatch) -> None:
    _install_httpx(
        monkeypatch,
        payload={},
        error=httpx.HTTPStatusError(
            "500", request=httpx.Request("POST", "http://opa"), response=httpx.Response(500)
        ),
    )
    allowed, _ = await OpaClient(_settings()).allow({"action": "chat"})
    assert allowed is False


@pytest.mark.parametrize("malformed", ["yes", 1, []])
async def test_malformed_result_fails_closed(monkeypatch, malformed) -> None:
    """策略改了返回结构时不能被解释成"放行"。"""
    _install_httpx(monkeypatch, payload={"result": malformed})
    allowed, reason = await OpaClient(_settings()).allow({"action": "chat"})
    assert allowed is False
    assert reason == "opa malformed response"


async def test_missing_result_field_fails_closed(monkeypatch) -> None:
    _install_httpx(monkeypatch, payload={"decision_id": "abc"})
    allowed, reason = await OpaClient(_settings()).allow({"action": "chat"})
    assert allowed is False
    assert reason == "opa malformed response"


async def test_request_targets_package_path_with_input(monkeypatch) -> None:
    calls = _install_httpx(monkeypatch, payload={"result": True})
    await OpaClient(_settings()).allow({"action": "chat", "user": {"user_id": "u_1"}})
    url, body = calls[0]
    assert url == "http://opa:8181/v1/data/rag"
    assert body["input"]["user"]["user_id"] == "u_1"


def test_enabled_follows_settings_unless_overridden() -> None:
    assert OpaClient(_settings()).enabled is True
    assert OpaClient(_settings(authz_enabled=False)).enabled is False
    assert OpaClient(_settings(), enabled=True).enabled is True
