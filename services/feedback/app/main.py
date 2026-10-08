"""feedback 入口：提交反馈、查阅反馈、导出坏例。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request

from app.bad_cases import BadCaseCollector
from app.config import Settings
from app.consumers import consume_feedback_events
from app.store import FeedbackStore
from packages.common.background import cancel_and_wait, spawn_supervised
from packages.common.constants import SERVICE_FEEDBACK, VERSION
from packages.common.errors import install_exception_handlers
from packages.common.logging import get_logger, setup_logging
from packages.common.settings import load_settings
from packages.contracts import (
    FeedbackRequest,
    FeedbackResponse,
    HealthResponse,
)
from packages.observability import init_otel
from packages.observability.metrics import install_metrics

settings: Settings = load_settings(Settings)

router = APIRouter(prefix="/feedback", tags=["feedback"])


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(settings.service_name, settings.log_level)
    logger = get_logger(settings.service_name)
    init_otel(settings.service_name, settings.otel_endpoint, settings.otel_enabled)

    store = FeedbackStore(settings.database_url)
    app.state.store = store
    app.state.collector = BadCaseCollector(store, settings.bad_case_dir)

    consumer_task: asyncio.Task | None = None
    try:
        if settings.use_kafka:
            consumer_task = spawn_supervised(
                consume_feedback_events(store, settings), name="feedback-kafka-consumer"
            )
        logger.info("feedback 启动 port=%s", settings.port)
        yield
    finally:
        await cancel_and_wait(consumer_task)


app = FastAPI(title="feedback", version=VERSION, lifespan=lifespan)
install_exception_handlers(app)
install_metrics(app, SERVICE_FEEDBACK, settings=settings)


def _store(request: Request) -> FeedbackStore:
    store = getattr(request.app.state, "store", None)
    if store is None:  # pragma: no cover
        raise HTTPException(status_code=503, detail="feedback store 未初始化")
    return store


@router.post("", response_model=FeedbackResponse, summary="提交反馈")
async def submit(req: FeedbackRequest, request: Request) -> FeedbackResponse:
    tenant_id = request.headers.get("x-tenant-id") or "default"
    user_id = request.headers.get("x-user-id") or ""
    feedback_id = await _store(request).add(
        tenant_id=tenant_id,
        user_id=user_id,
        query=req.query,
        answer=req.answer,
        rating=req.rating,
        comment=req.comment,
        trace_id=req.trace_id,
    )
    return FeedbackResponse(id=feedback_id)


@router.get("", summary="最近反馈")
async def list_feedback(request: Request, limit: int = 50, tenant_id: str = "") -> dict[str, Any]:
    items = await _store(request).list_recent(tenant_id=tenant_id or None, limit=min(limit, 500))
    return {"total": len(items), "items": items}


@router.get("/bad-cases", summary="坏例列表")
async def bad_cases(request: Request, limit: int = 100, tenant_id: str = "") -> dict[str, Any]:
    collector: BadCaseCollector = request.app.state.collector
    items = await collector.collect(limit=min(limit, 500), tenant_id=tenant_id or None)
    return {"total": len(items), "items": items}


@router.post("/bad-cases/export", summary="导出坏例为评测数据集")
async def export_bad_cases(
    request: Request, limit: int = 100, tenant_id: str = ""
) -> dict[str, str]:
    collector: BadCaseCollector = request.app.state.collector
    path = await collector.export(limit=min(limit, 500), tenant_id=tenant_id or None)
    return {"path": path.as_posix()}


app.include_router(router)


@app.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    ok, message = await _store(request).health()
    return HealthResponse(
        status="ok" if ok else "degraded",
        service=SERVICE_FEEDBACK,
        version=VERSION,
        details={"postgres": message, "bad_case_dir": settings.bad_case_dir},
    )


def run() -> None:  # pragma: no cover
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
