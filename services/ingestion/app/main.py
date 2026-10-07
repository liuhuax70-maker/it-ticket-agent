"""ingestion 入口：/ingest、/ingest/upload、/jobs/{job_id}。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, UploadFile

from app.config import Settings
from app.consumers import consume_raw_documents
from app.service import IngestionService
from packages.common.constants import VERSION
from packages.common.errors import NotFoundError, install_exception_handlers
from packages.common.logging import get_logger, setup_logging
from packages.common.settings import load_settings
from packages.contracts import ACL, HealthResponse, IngestRequest, IngestResponse, Visibility
from packages.observability import init_otel

settings: Settings = load_settings(Settings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(settings.service_name, settings.log_level)
    logger = get_logger(settings.service_name)
    init_otel(settings.service_name, settings.otel_endpoint, settings.otel_enabled)

    service = IngestionService(settings)
    app.state.service = service

    consumer_task: asyncio.Task | None = None
    try:
        await service.startup()
        if settings.use_kafka:
            consumer_task = asyncio.create_task(consume_raw_documents(service, settings))
        logger.info("ingestion 启动完成 port=%s", settings.port)
        yield
    finally:
        if consumer_task is not None:
            consumer_task.cancel()
        await service.aclose()


app = FastAPI(title="ingestion", version=VERSION, lifespan=lifespan)
install_exception_handlers(app)


def _service() -> IngestionService:
    service = getattr(app.state, "service", None)
    if service is None:  # pragma: no cover
        raise HTTPException(status_code=503, detail="ingestion service 未初始化")
    return service


@app.post("/ingest", response_model=IngestResponse)
async def ingest(req: IngestRequest) -> IngestResponse:
    """按路径（文件或目录）或直接传文本接入。"""
    return await _service().ingest(req)


@app.post("/ingest/upload", response_model=IngestResponse)
async def ingest_upload(
    file: Annotated[UploadFile, File()],
    tenant_id: Annotated[str, Form()] = "",
    department_id: Annotated[str, Form()] = "",
    visibility: Annotated[str, Form()] = "internal",
    reindex: Annotated[bool, Form()] = False,
) -> IngestResponse:
    data = await file.read()
    if not data:
        raise HTTPException(status_code=422, detail="上传文件为空")
    acl = ACL(
        tenant_id=tenant_id or settings.default_tenant_id,
        department_id=department_id or settings.default_department_id,
        visibility=Visibility(visibility),
    )
    return await _service().ingest_bytes(file.filename or "upload.bin", data, acl=acl, reindex=reindex)


@app.get("/jobs/{job_id}")
async def get_job(job_id: str) -> dict[str, object]:
    job = await _service()._metadata.get_job(job_id)  # noqa: SLF001 - 只读查询，无需再包一层
    if job is None:
        raise NotFoundError(f"任务不存在: {job_id}")
    return job


@app.get("/stats")
async def stats() -> dict[str, object]:
    """文档/分块总量，供管理面与验收脚本使用。"""
    return await _service()._metadata.stats()  # noqa: SLF001


@app.get("/documents")
async def list_documents(
    limit: int = 100,
    offset: int = 0,
    tenant_id: str = "",
) -> dict[str, object]:
    """文档台账（按创建时间倒序）。供增量同步与留存清理任务使用。"""
    rows = await _service()._metadata.list_documents(  # noqa: SLF001
        limit=min(max(limit, 1), 500), offset=max(offset, 0), tenant_id=tenant_id or None
    )
    return {"total": len(rows), "items": rows}


@app.get("/documents/{doc_id}")
async def get_document(doc_id: str) -> dict[str, object]:
    document = await _service()._metadata.get_document(doc_id)  # noqa: SLF001
    if document is None:
        raise NotFoundError(f"文档不存在: {doc_id}")
    return document


@app.delete("/documents/{doc_id}")
async def delete_document(doc_id: str) -> dict[str, object]:
    """合规删除：同时清检索索引与元数据（幂等）。"""
    return await _service().delete_document(doc_id)


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
