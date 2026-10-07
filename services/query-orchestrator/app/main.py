"""query-orchestrator 入口：POST /chat。

身份来源：默认走请求头 ``X-Tenant-Id / X-Department-Id / X-User-Id / X-User-Roles``
（由 api-gateway 注入）；网关未启用鉴权时会填默认身份。
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request

from app.config import Settings
from app.service import OrchestratorService
from packages.common.constants import SERVICE_QUERY_ORCHESTRATOR, VERSION
from packages.common.errors import install_exception_handlers
from packages.common.logging import get_logger, setup_logging
from packages.common.settings import load_settings
from packages.contracts import ChatRequest, ChatResponse, HealthResponse
from packages.observability import init_otel
from packages.security import Identity

settings: Settings = load_settings(Settings)


def identity_from_headers(request: Request) -> Identity:
    roles_header = request.headers.get("x-user-roles", "")
    return Identity(
        user_id=request.headers.get("x-user-id") or settings.default_user_id,
        tenant_id=request.headers.get("x-tenant-id") or settings.default_tenant_id,
        department_id=request.headers.get("x-department-id") or settings.default_department_id,
        roles=[r.strip() for r in roles_header.split(",") if r.strip()],
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(settings.service_name, settings.log_level)
    logger = get_logger(settings.service_name)
    init_otel(settings.service_name, settings.otel_endpoint, settings.otel_enabled)

    service = OrchestratorService(settings)
    app.state.service = service
    logger.info(
        "query-orchestrator 启动 port=%s retrieval=%s model_gateway=%s",
        settings.port,
        settings.retrieval_url,
        settings.model_gateway_url,
    )
    try:
        yield
    finally:
        await service.aclose()


app = FastAPI(title="query-orchestrator", version=VERSION, lifespan=lifespan)
install_exception_handlers(app)


def _service() -> OrchestratorService:
    service = getattr(app.state, "service", None)
    if service is None:  # pragma: no cover
        raise HTTPException(status_code=503, detail="orchestrator service 未初始化")
    return service


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, request: Request) -> ChatResponse:
    return await _service().chat(req, identity_from_headers(request))


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    details = await _service().health()
    status = details.pop("status")
    return HealthResponse(
        status=status,  # type: ignore[arg-type]
        service=settings.service_name,
        version=VERSION,
        details={k: str(v) for k, v in details.items()},
    )


def run() -> None:  # pragma: no cover
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)


__all__ = ["app", "identity_from_headers", "SERVICE_QUERY_ORCHESTRATOR"]
