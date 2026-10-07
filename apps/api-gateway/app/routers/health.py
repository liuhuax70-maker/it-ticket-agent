"""健康检查：网关自身 + 下游可达性。

下游不可达时整体为 ``degraded`` 而不是 ``error``：
网关进程是健康的，只是依赖不全——这个区分直接影响告警该打给谁。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from packages.common.constants import SERVICE_API_GATEWAY, VERSION
from packages.contracts import HealthResponse

router = APIRouter(tags=["health"])


async def _probe(client: Any) -> str:
    try:
        return "ok" if await client.ping() else "unreachable"
    except Exception as exc:  # noqa: BLE001
        return f"error: {exc}"


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    state = request.app.state
    settings = state.settings

    details: dict[str, str] = {
        "authz_enabled": str(settings.authz_enabled),
        "rate_limit_enabled": str(settings.rate_limit_enabled),
        "query_orchestrator": await _probe(state.orchestrator),
        "ingestion": await _probe(state.ingestion),
        "model_gateway": await _probe(state.model_gateway),
    }

    status = "ok" if all(v == "ok" for v in details.values()) else "degraded"
    return HealthResponse(
        status=status,  # type: ignore[arg-type]
        service=SERVICE_API_GATEWAY,
        version=VERSION,
        details=details,
    )
