"""策略包可见性与 fail-closed 行为测试。"""

from __future__ import annotations

import app.main as main_module
from fastapi.testclient import TestClient


def test_policy_bundle_is_discoverable() -> None:
    policies = main_module.inspect_policies()
    assert policies, "未发现任何 .rego 策略文件"
    rag = next(p for p in policies if p["file"].endswith("rag.rego"))
    assert rag["package"] == "rag"
    assert {"allow", "reason", "is_admin"} <= set(rag["rules"])
    assert len(rag["sha256"]) == 16


def test_rego_declares_default_deny() -> None:
    """默认拒绝是这套策略的安全基线，回归时必须守住。"""
    policies = main_module.inspect_policies()
    rag_path = next(p["file"] for p in policies if p["file"].endswith("rag.rego"))
    content = (main_module.Path(rag_path)).read_text(encoding="utf-8")
    assert "default allow = false" in content
    assert "tenant_missing" in content  # 租户缺失必须被识别为拒绝原因


def test_decision_fails_closed_when_opa_unreachable() -> None:
    original_url, original_timeout = main_module.settings.opa_url, main_module.settings.opa_timeout
    main_module.settings.opa_url = "http://127.0.0.1:9"
    main_module.settings.opa_timeout = 0.2
    try:
        with TestClient(main_module.app) as client:
            resp = client.post(
                "/decision",
                json={"action": "chat", "user": {"tenant_id": "t1", "roles": ["rag_user"]}},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["allowed"] is False, "OPA 不可达时必须拒绝（fail-closed）"
        assert "opa unavailable" in body["reason"]
    finally:
        main_module.settings.opa_url = original_url
        main_module.settings.opa_timeout = original_timeout


def test_policies_endpoint_lists_bundle() -> None:
    with TestClient(main_module.app) as client:
        body = client.get("/policies").json()
    assert body["count"] >= 1
    assert any(p["file"].endswith("rag.rego") for p in body["policies"])
