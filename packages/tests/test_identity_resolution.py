"""身份解析：从请求头到 Identity 的每一步都必须是确定的。

这里的失败模式都很安静：缺 claim 时若退化成空串，ACL 的租户约束就形同虚设；
方案判断若写成大小写敏感，"Bearer" 与 "bearer" 会走向完全不同的分支。
"""

from __future__ import annotations

from typing import Any

import pytest

from packages.security.config import SecuritySettings
from packages.security.identity import Unauthorized, resolve_identity


def _request(headers: dict[str, str]) -> Any:
    """构造一个只带 headers 的替身请求。

    返回 Any 是刻意的：resolve_identity 要的是 Starlette 的 Request，
    而我们只需要它的 headers 属性。用 Any 避免在每个调用点写 cast。
    """

    class _FakeRequest:
        def __init__(self) -> None:
            self.headers = headers

    return _FakeRequest()


def _patch_claims(monkeypatch, claims: dict) -> None:
    monkeypatch.setattr("packages.security.identity.decode_keycloak_token", lambda *_: claims)


def test_authz_disabled_returns_fixed_identity() -> None:
    """关闭鉴权时给固定身份，且不解析请求头。"""
    settings = SecuritySettings(
        authz_enabled=False, default_user_id="u_fixed", default_tenant_id="t_fixed"
    )
    identity = resolve_identity(_request({}), settings)
    assert identity.user_id == "u_fixed"
    assert identity.tenant_id == "t_fixed"
    assert identity.roles == ["rag_user"]


def test_missing_authorization_header_is_rejected() -> None:
    with pytest.raises(Unauthorized, match="Bearer"):
        resolve_identity(_request({}), SecuritySettings(authz_enabled=True))


def test_non_bearer_scheme_is_rejected() -> None:
    """Basic 之类的方案不能混过去。"""
    with pytest.raises(Unauthorized, match="Bearer"):
        resolve_identity(
            _request({"authorization": "Basic dXNlcg=="}), SecuritySettings(authz_enabled=True)
        )


def test_identity_is_built_from_claims(monkeypatch) -> None:
    _patch_claims(
        monkeypatch,
        {
            "sub": "u_alice",
            "tenant_id": "tenant-a",
            "department_id": "hr",
            "realm_access": {"roles": ["rag_user", "rag_writer"]},
            "email": "alice@example.com",
        },
    )
    identity = resolve_identity(
        _request({"authorization": "Bearer fake.jwt"}), SecuritySettings(authz_enabled=True)
    )
    assert identity.user_id == "u_alice"
    assert identity.tenant_id == "tenant-a"
    assert identity.department_id == "hr"
    assert identity.roles == ["rag_user", "rag_writer"]
    assert identity.email == "alice@example.com"
    assert identity.is_admin is False


def test_rag_admin_role_sets_is_admin(monkeypatch) -> None:
    _patch_claims(monkeypatch, {"sub": "u_admin", "realm_access": {"roles": ["rag_admin"]}})
    identity = resolve_identity(
        _request({"authorization": "Bearer x"}), SecuritySettings(authz_enabled=True)
    )
    assert identity.is_admin is True


def test_missing_tenant_claim_falls_back_to_default(monkeypatch) -> None:
    """缺 tenant 时退到默认租户，而不是空串——空串会让 ACL 的租户约束失效。"""
    settings = SecuritySettings(authz_enabled=True, default_tenant_id="default")
    _patch_claims(monkeypatch, {"sub": "u"})
    identity = resolve_identity(_request({"authorization": "Bearer x"}), settings)
    assert identity.tenant_id == "default"
    assert identity.department_id == settings.default_department_id


def test_missing_sub_falls_back_to_unknown(monkeypatch) -> None:
    settings = SecuritySettings(authz_enabled=True)
    _patch_claims(monkeypatch, {})
    identity = resolve_identity(_request({"authorization": "Bearer x"}), settings)
    assert identity.user_id == "unknown"


def test_bearer_scheme_is_case_insensitive_and_token_is_trimmed(monkeypatch) -> None:
    seen: dict[str, str] = {}

    def _capture(token: str, _settings):
        seen["token"] = token
        return {"sub": "u"}

    monkeypatch.setattr("packages.security.identity.decode_keycloak_token", _capture)
    resolve_identity(
        _request({"authorization": "Bearer   spaced.token   "}),
        SecuritySettings(authz_enabled=True),
    )
    assert seen["token"] == "spaced.token"


def test_jwks_cache_is_keyed_by_url(monkeypatch) -> None:
    """多 realm 共用进程时，不同 jwks_url 的公钥必须分键缓存、互不覆盖。

    否则 A realm 的公钥会被拿去验 B realm 的令牌，多租户/多 IdP 场景下验签结果不可信。
    """
    import httpx

    from packages.security.identity import _fetch_jwks

    calls: dict[str, int] = {}

    class _Resp:
        def __init__(self, keys: list[dict[str, Any]]) -> None:
            self._keys = keys

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"keys": self._keys}

    def _fake_get(target_url: str, timeout: float = 5.0) -> _Resp:
        calls[target_url] = calls.get(target_url, 0) + 1
        if "realm-a" in target_url:
            return _Resp([{"kid": "a", "k": "ka"}])
        return _Resp([{"kid": "b", "k": "kb"}])

    monkeypatch.setattr(httpx, "get", _fake_get)

    settings_a = SecuritySettings(authz_enabled=True, keycloak_realm="realm-a")
    settings_b = SecuritySettings(authz_enabled=True, keycloak_realm="realm-b")

    keys_a = _fetch_jwks(settings_a)
    keys_b = _fetch_jwks(settings_b)
    assert keys_a[0]["kid"] == "a"
    assert keys_b[0]["kid"] == "b"

    # 二次调用应命中各自缓存，不再发请求（每个 url 仅请求一次）
    _fetch_jwks(settings_a)
    _fetch_jwks(settings_b)
    assert calls[settings_a.jwks_url()] == 1
    assert calls[settings_b.jwks_url()] == 1
