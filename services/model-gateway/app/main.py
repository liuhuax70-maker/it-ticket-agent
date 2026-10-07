"""model-gateway 入口：模型调用、向量化、模型清单、健康检查。"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from app.config import Settings
from app.litellm_config import declared_models
from app.router import ModelRouter
from packages.common.constants import VERSION
from packages.common.errors import ConfigError, install_exception_handlers
from packages.common.logging import get_logger, setup_logging
from packages.common.settings import load_settings
from packages.contracts import (
    CompletionRequest,
    EmbedRequest,
    EmbedResponse,
    GenerateRequest,
    GenerateResponse,
    HealthResponse,
    ModelInfo,
)
from packages.observability import Tracer, init_otel

settings: Settings = load_settings(Settings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(settings.service_name, settings.log_level)
    logger = get_logger(settings.service_name)
    # ⚠️ 只传了 service_name：init_otel 的 endpoint/enabled 有默认值（enabled=False），
    # 所以本服务的 OTel 实际是关闭的，其他 5 个服务都传了完整三参。
    # 要开启请改成 init_otel(settings.service_name, settings.otel_endpoint, settings.otel_enabled)。
    init_otel(settings.service_name)
    app.state.tracer = Tracer(
        host=settings.langfuse_host,
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        service=settings.service_name,
    )
    app.state.router = ModelRouter(settings)
    logger.info(
        "model-gateway 启动: provider=%s model=%s fallbacks=%s",
        settings.llm_provider,
        settings.deepseek_model if settings.llm_provider == "deepseek" else settings.local_llm_model,
        settings.fallback_list(),
    )
    try:
        yield
    finally:
        await app.state.router.aclose()


app = FastAPI(title="model-gateway", version=VERSION, lifespan=lifespan)
install_exception_handlers(app)


def _router() -> ModelRouter:
    router = getattr(app.state, "router", None)
    if router is None:  # pragma: no cover - 仅在未走 lifespan 时发生
        raise HTTPException(status_code=503, detail="model router 未初始化")
    return router


@app.post("/generate", response_model=GenerateResponse)
async def generate(req: GenerateRequest) -> GenerateResponse:
    return await _router().generate(req)


@app.post("/complete", response_model=GenerateResponse)
async def complete(req: CompletionRequest) -> GenerateResponse:
    """原始补全：调用方自带 prompt（查询改写、合规审核）。"""
    return await _router().complete(req)


@app.post("/embed", response_model=EmbedResponse)
async def embed(req: EmbedRequest) -> EmbedResponse:
    return await _router().embed(req)


@app.get("/quota/{tenant_id}")
async def quota(tenant_id: str) -> dict[str, object]:
    """租户当日 token 用量（仅供管理面查看）。"""
    snapshot = await _router().quota_snapshot(tenant_id)
    return {"tenant_id": tenant_id, **snapshot}


@app.get("/models", response_model=list[ModelInfo])
async def models() -> list[ModelInfo]:
    router = _router()
    active = [
        ModelInfo(
            name=t.name,
            provider=t.provider,
            kind="chat",
            available=True,
            note="当前生效" if idx == 0 else "兜底",
        )
        for idx, t in enumerate(router.targets())
    ]
    declared = declared_models(settings.litellm_config_path)
    known = {m.name for m in active}
    return active + [m for m in declared if m.name not in known]


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    details: dict[str, str] = {}
    status = "ok"
    try:
        router = _router()
        chain = router.targets()
        details["primary"] = f"{chain[0].provider}:{chain[0].name}"
        details["fallbacks"] = ",".join(t.name for t in chain[1:]) or "none"
    except ConfigError as exc:
        status = "degraded"
        details["config"] = str(exc)
    details["embed_backend"] = f"{settings.embed_backend}:{settings.embed_model}"
    details["quota_enforced"] = str(settings.quota_enabled)
    return HealthResponse(status=status, service=settings.service_name, version=VERSION, details=details)


def run() -> None:  # pragma: no cover - 手工启动
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
