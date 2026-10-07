"""authz 入口：策略包可视化 + 决策评估 + 健康检查。

边界说明：**策略执行点在 api-gateway**（它持有用户身份）。
本服务是控制面：暴露策略包状态、提供决策评估通道（转发 OPA）与排障入口，
不参与请求链路，因此它挂掉不影响问答可用性，只是无法在线排障。
"""

from __future__ import annotations

import hashlib
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi import Request
from pydantic import BaseModel, Field

from packages.common.constants import SERVICE_AUTHZ, VERSION
from packages.common.errors import install_exception_handlers
from packages.common.logging import get_logger, setup_logging
from packages.common.settings import load_settings
from packages.contracts import HealthResponse
from packages.observability import init_otel

from app.config import Settings

settings: Settings = load_settings(Settings)

# 顶层规则名：`allow {` / `reason = ... {` / `default allow = false`
_RULE = re.compile(r"^(?:default\s+)?([a-z_][a-zA-Z0-9_]*)\s*(?:\(|=|\{|if\b)", re.MULTILINE)


class DecisionRequest(BaseModel):
    action: str
    user: dict[str, Any] = Field(default_factory=dict)
    resource: dict[str, Any] = Field(default_factory=dict)
    tenant_id: str | None = None


def policy_files() -> list[Path]:
    directory = Path(settings.policies_dir)
    if not directory.exists():
        return []
    return sorted(directory.rglob("*.rego"))


def inspect_policies() -> list[dict[str, Any]]:
    """策略包清单：文件名 + 摘要 + 顶层规则名。

    摘要是必要的：改了一行策略却没人知道改了什么，是权限事故的常见起点。
    """
    result: list[dict[str, Any]] = []
    for path in policy_files():
        content = path.read_text(encoding="utf-8")
        result.append(
            {
                "file": path.as_posix(),
                "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()[:16],
                "package": next(
                    (line.split()[1] for line in content.splitlines() if line.startswith("package ")),
                    "",
                ),
                "rules": sorted(set(_RULE.findall(content))),
                "lines": len(content.splitlines()),
            }
        )
    return result


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(settings.service_name, settings.log_level)
    logger = get_logger(settings.service_name)
    init_otel(settings.service_name, settings.otel_endpoint, settings.otel_enabled)
    app.state.settings = settings
    logger.info(
        "authz 启动 port=%s policies=%s opa=%s",
        settings.port,
        len(policy_files()),
        settings.opa_url,
    )
    yield


app = FastAPI(title="authz", version=VERSION, lifespan=lifespan)
install_exception_handlers(app)


@app.get("/policies")
async def policies() -> dict[str, Any]:
    return {"count": len(policy_files()), "policies": inspect_policies()}


@app.post("/decision")
async def decision(req: DecisionRequest) -> dict[str, Any]:
    """转发到 OPA 评估。OPA 不可达时返回 allowed=false（fail-closed）。"""
    payload = {
        "input": {
            "action": req.action,
            "user": req.user,
            "resource": req.resource,
            "tenant_id": req.tenant_id,
        }
    }
    async with httpx.AsyncClient(timeout=settings.opa_timeout) as client:
        try:
            allow_resp = await client.post(
                f"{settings.opa_url.rstrip('/')}/{settings.decision_path}", json=payload
            )
            allow_resp.raise_for_status()
            allowed = bool(allow_resp.json().get("result", False))

            reason_resp = await client.post(
                f"{settings.opa_url.rstrip('/')}/{settings.reason_path}", json=payload
            )
            reason = reason_resp.json().get("result", "") if reason_resp.status_code < 400 else ""
        except Exception as exc:  # noqa: BLE001
            return {"allowed": False, "reason": f"opa unavailable: {exc}", "source": "authz"}

    return {"allowed": allowed, "reason": reason, "source": "opa"}


@app.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    details: dict[str, str] = {
        "policies": str(len(policy_files())),
        "opa_url": settings.opa_url,
        "keycloak_realm": settings.keycloak_realm,
    }
    try:
        async with httpx.AsyncClient(timeout=settings.opa_timeout) as client:
            resp = await client.get(f"{settings.opa_url.rstrip('/')}/health")
            details["opa"] = "ok" if resp.status_code == 200 else f"http {resp.status_code}"
    except Exception as exc:  # noqa: BLE001
        details["opa"] = f"unreachable: {exc}"

    status = "ok" if details["opa"] == "ok" and policy_files() else "degraded"
    return HealthResponse(
        status=status,  # type: ignore[arg-type]
        service=SERVICE_AUTHZ,
        version=VERSION,
        details=details,
    )


def run() -> None:  # pragma: no cover
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
