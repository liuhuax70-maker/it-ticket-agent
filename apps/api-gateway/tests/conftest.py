"""把服务目录加入 sys.path，使 ``app`` 包可被测试导入。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))


@pytest.fixture()
def client(monkeypatch):
    """共享的网关测试客户端：假下游、限流开启、审计不落库。

    放在 conftest 而不是 test_gateway：test_audit_sink 也要用同一套夹具，
    而 pytest 的 fixture 只在同目录 conftest 或导入处可见。
    """
    import app.main as main_module
    from app.config import Settings
    from test_gateway import (
        FakeCounter,
        FakeFeedback,
        FakeIngestion,
        FakeModelGateway,
        FakeOrchestrator,
    )

    monkeypatch.setattr(main_module, "RedisCounter", FakeCounter)
    app = main_module.create_app(
        Settings(
            rate_limit_enabled=True,
            rate_limit_per_minute=3,
            serve_ui=False,
            audit_enabled=True,
            audit_database_url="",
            authz_enabled=False,
            cors_origins="",
        )
    )
    with TestClient(app) as test_client:
        app.state.orchestrator = FakeOrchestrator()
        app.state.ingestion = FakeIngestion()
        app.state.model_gateway = FakeModelGateway()
        app.state.feedback = FakeFeedback()
        test_client.app = app
        yield test_client
