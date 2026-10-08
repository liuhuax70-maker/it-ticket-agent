"""retrieval 入口：POST /search、POST /rerank。"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from app.config import Settings
from app.service import RetrievalService
from packages.common.constants import SERVICE_RETRIEVAL, VERSION
from packages.common.errors import install_exception_handlers
from packages.common.logging import get_logger, setup_logging
from packages.common.settings import load_settings
from packages.contracts import (
    HealthResponse,
    RerankRequest,
    RerankResponse,
    SearchRequest,
    SearchResponse,
)
from packages.observability import init_otel
from packages.observability.metrics import install_metrics

settings: Settings = load_settings(Settings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：初始化日志/OTel，构造并启动 ``RetrievalService``，退出时释放连接。"""
    setup_logging(settings.service_name, settings.log_level)
    logger = get_logger(settings.service_name)
    init_otel(settings.service_name, settings.otel_endpoint, settings.otel_enabled)

    service = RetrievalService(settings)
    app.state.service = service
    try:
        await service.startup()
        logger.info("retrieval 启动完成 port=%s", settings.port)
        yield
    finally:
        await service.aclose()


app = FastAPI(title="retrieval", version=VERSION, lifespan=lifespan)
install_exception_handlers(app)
install_metrics(app, SERVICE_RETRIEVAL, settings=settings)


def _service() -> RetrievalService:
    service = getattr(app.state, "service", None)
    if service is None:  # pragma: no cover
        raise HTTPException(status_code=503, detail="retrieval service 未初始化")
    return service


@app.post("/search", response_model=SearchResponse)
async def search(req: SearchRequest) -> SearchResponse:
    """POST /search：召回 + 融合（可选重排），返回 ``SearchResponse``。"""
    return await _service().search(req)


@app.post("/rerank", response_model=RerankResponse)
async def rerank(req: RerankRequest) -> RerankResponse:
    """POST /rerank：对给定命中列表按 query 重排，返回 ``RerankResponse``。"""
    return await _service().rerank(req)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """GET /health：聚合 Milvus/OpenSearch 健康与行数，返回 ``HealthResponse``。"""
    details = await _service().health()
    status = details.pop("status")
    return HealthResponse(
        status=status,  # type: ignore[arg-type]
        service=settings.service_name,
        version=VERSION,
        details={k: str(v) for k, v in details.items()},
    )


def run() -> None:  # pragma: no cover
    """本地启动入口（uvicorn）。"""
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
