"""indexing 入口：/index 与 /documents/{doc_id}/delete。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from packages.common.constants import SERVICE_INDEXING, VERSION
from packages.common.errors import install_exception_handlers
from packages.common.logging import get_logger, setup_logging
from packages.common.settings import load_settings
from packages.contracts import HealthResponse, IndexRequest, IndexResponse
from packages.observability import init_otel

from app.config import Settings
from app.consumers import consume_chunk_events
from app.service import IndexService

settings: Settings = load_settings(Settings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(settings.service_name, settings.log_level)
    logger = get_logger(settings.service_name)
    init_otel(settings.service_name, settings.otel_endpoint, settings.otel_enabled)

    service = IndexService(settings)
    app.state.service = service

    consumer_task: asyncio.Task | None = None
    try:
        await service.startup()
        if settings.use_kafka:
            consumer_task = asyncio.create_task(consume_chunk_events(service, settings))
        logger.info("indexing 启动完成 port=%s", settings.port)
        yield
    finally:
        if consumer_task is not None:
            consumer_task.cancel()
        await service.aclose()


app = FastAPI(title="indexing", version=VERSION, lifespan=lifespan)
install_exception_handlers(app)


def _service() -> IndexService:
    service = getattr(app.state, "service", None)
    if service is None:  # pragma: no cover
        raise HTTPException(status_code=503, detail="indexing service 未初始化")
    return service


@app.post("/index", response_model=IndexResponse)
async def index(req: IndexRequest) -> IndexResponse:
    return await _service().index(req)


@app.post("/documents/{doc_id}/delete")
async def delete_document(doc_id: str) -> dict[str, object]:
    counts = await _service().delete_document(doc_id)
    return {"doc_id": doc_id, "deleted": counts}


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
