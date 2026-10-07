"""文档路由：接入、查询、合规删除。

注意路由顺序：``/jobs/{job_id}`` 必须声明在 ``/{doc_id}`` 之前，
否则 "jobs" 会被当成 doc_id 吃掉。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from packages.contracts import ACL, IngestRequest, IngestResponse, Visibility
from packages.security import Identity

from app.clients.ingestion import IngestionClient
from app.config import Settings
from app.middleware.identity import require_action

router = APIRouter(prefix="/documents", tags=["documents"])


def _client(request: Request) -> IngestionClient:
    return request.app.state.ingestion


def _settings(request: Request) -> Settings:
    return request.app.state.settings


@router.post("/ingest", response_model=IngestResponse, summary="按路径或文本接入")
async def ingest(
    req: IngestRequest,
    request: Request,
    identity: Identity = Depends(require_action("documents:write")),
) -> IngestResponse:
    # 未显式指定 ACL 时，默认归属调用方租户/部门
    if req.acl is None:
        req = req.model_copy(
            update={"acl": ACL(tenant_id=identity.tenant_id, department_id=identity.department_id)}
        )
    return await _client(request).ingest(req)


@router.post("/upload", response_model=IngestResponse, summary="上传文件接入")
async def upload(
    request: Request,
    file: UploadFile = File(...),
    visibility: str = Form(default="internal"),
    reindex: bool = Form(default=False),
    identity: Identity = Depends(require_action("documents:write")),
) -> IngestResponse:
    data = await file.read()
    if not data:
        raise HTTPException(status_code=422, detail="上传文件为空")

    settings = _settings(request)
    limit_bytes = 32 * 1024 * 1024
    if len(data) > limit_bytes:
        raise HTTPException(status_code=413, detail=f"文件超过 {limit_bytes // 1024 // 1024}MB 上限")

    acl = ACL(
        tenant_id=identity.tenant_id,
        department_id=identity.department_id,
        visibility=Visibility(visibility),
        owner=identity.user_id,
    )
    return await _client(request).upload(file.filename or "upload.bin", data, acl=acl, reindex=reindex)


@router.get("/jobs/{job_id}", summary="查询接入任务")
async def get_job(
    job_id: str,
    request: Request,
    identity: Identity = Depends(require_action("documents:read")),  # noqa: ARG001
) -> dict:
    return await _client(request).get_job(job_id)


@router.get("/{doc_id}", summary="查询文档元数据")
async def get_document(
    doc_id: str,
    request: Request,
    identity: Identity = Depends(require_action("documents:read")),  # noqa: ARG001
) -> dict:
    return await _client(request).get_document(doc_id)


@router.delete("/{doc_id}", summary="合规删除文档")
async def delete_document(
    doc_id: str,
    request: Request,
    identity: Identity = Depends(require_action("documents:delete")),  # noqa: ARG001
) -> dict:
    return await _client(request).delete_document(doc_id)
