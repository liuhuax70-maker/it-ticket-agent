"""api-gateway 入口。

中间件顺序（starlette：**越晚添加越靠外层**）：
    Audit(审计) -> Identity(身份) -> RateLimit(限流) -> 路由
先审计再鉴权，是为了让 401/429 这类被拒请求同样留有访问记录。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.clients import (
    FeedbackClient,
    IngestionClient,
    KeycloakClient,
    ModelGatewayClient,
    OpaClient,
    OrchestratorClient,
    RedisCounter,
)
from app.config import Settings
from app.middleware import AuditMiddleware, IdentityMiddleware, RateLimitMiddleware
from app.routers import admin, chat, documents, feedback, health
from packages.common.constants import SERVICE_API_GATEWAY, VERSION
from packages.common.errors import install_exception_handlers
from packages.common.logging import get_logger, setup_logging
from packages.common.settings import load_settings
from packages.observability import init_otel
from packages.observability.metrics import install_metrics

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings(Settings)
    counter = RedisCounter(settings.redis_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        setup_logging(settings.service_name, settings.log_level)
        logger = get_logger(settings.service_name)
        init_otel(settings.service_name, settings.otel_endpoint, settings.otel_enabled)

        app.state.settings = settings
        app.state.counter = counter
        app.state.orchestrator = OrchestratorClient(
            settings.query_orchestrator_url, settings.request_timeout
        )
        app.state.ingestion = IngestionClient(settings.ingestion_url, settings.ingest_timeout)
        app.state.model_gateway = ModelGatewayClient(settings.model_gateway_url)
        app.state.feedback = FeedbackClient(settings.feedback_url)
        app.state.opa = OpaClient(settings)
        app.state.keycloak = KeycloakClient(settings)
        # 审计落库：只在配置了数据库 URL 时启用；启动失败不拦服务（合规旁路，
        # stdout 审计仍在），但要在日志里响亮地说明，不能静默退化。
        audit_sink = None
        if settings.audit_enabled and settings.audit_database_url:
            from app.audit_sink import AuditSink

            audit_sink = AuditSink(settings.audit_database_url)
            try:
                await audit_sink.start()
            except Exception as exc:  # noqa: BLE001
                logger.error("审计落库初始化失败（本次运行仅 stdout 审计）: %s", exc)
                audit_sink = None
        app.state.audit_sink = audit_sink

        logger.info(
            "api-gateway 启动 port=%s authz=%s rate_limit=%s",
            settings.port,
            settings.authz_enabled,
            settings.rate_limit_enabled,
        )
        try:
            yield
        finally:
            for name in ("orchestrator", "ingestion", "model_gateway", "feedback"):
                client = getattr(app.state, name, None)
                if client is not None:
                    await client.aclose()
            await app.state.opa.aclose()
            if getattr(app.state, "audit_sink", None) is not None:
                await app.state.audit_sink.aclose()
            await counter.aclose()

    app = FastAPI(title="api-gateway", version=VERSION, lifespan=lifespan)
    install_exception_handlers(app)

    if settings.cors_list():
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_list(),
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["x-request-id"],
        )

    app.add_middleware(RateLimitMiddleware, settings=settings, counter=counter)
    app.add_middleware(IdentityMiddleware, settings=settings)
    app.add_middleware(AuditMiddleware, settings=settings)
    # 指标中间件放在最后（=最外层）：401/429 这类被拒请求也必须计入可用性与错误率，
    # 否则监控会漏掉最该告警的那部分流量。
    install_metrics(app, SERVICE_API_GATEWAY, settings=settings)

    for module in (health, chat, documents, admin, feedback):
        app.include_router(module.router)

    if settings.serve_ui and STATIC_DIR.exists():
        app.mount("/ui", StaticFiles(directory=str(STATIC_DIR), html=True), name="ui")

        @app.get("/", include_in_schema=False)
        async def root() -> RedirectResponse:  # pragma: no cover - 仅浏览器访问
            return RedirectResponse(url="/ui/")

    return app


app = create_app()


def run() -> None:  # pragma: no cover
    import uvicorn

    uvicorn.run(
        "app.main:app", host=app.state.settings.host, port=app.state.settings.port, reload=False
    )


__all__ = ["app", "create_app", "SERVICE_API_GATEWAY"]
