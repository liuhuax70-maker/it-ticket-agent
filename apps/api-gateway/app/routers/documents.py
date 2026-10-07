"""文档路由：接入、查询、合规删除。

注意路由顺序：``/jobs/{job_id}`` 必须声明在 ``/{doc_id}`` 之前，
否则 "jobs" 会被当成 doc_id 吃掉。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from app.clients.ingestion import IngestionClient
from app.config import Settings
from app.middleware.identity import require_action
from packages.contracts import ACL, IngestRequest, IngestResponse, Visibility
from packages.security import Identity

router = APIRouter(prefix="/documents", tags=["documents"])

DocumentWriter = Annotated[Identity, Depends(require_action("documents:write"))]
DocumentReader = Annotated[Identity, Depends(require_action("documents:read"))]
DocumentDeleter = Annotated[Identity, Depends(require_action("documents:delete"))]


def _client(request: Request) -> IngestionClient:
    return request.app.state.ingestion


def _settings(request: Request) -> Settings:
    return request.app.state.settings


@router.post("/ingest", response_model=IngestResponse, summary="按路径或文本接入")
async def ingest(
    req: IngestRequest, request: Request, identity: DocumentWriter
) -> IngestResponse:
    # ACL 组装规则：**租户与部门只能来自身份**，调用方只被允许选择可见范围。
    # 否则客户端可以把自己的文档塞进别的租户（越权写入）。
    requested = req.acl
    acl = ACL(
        tenant_id=identity.tenant_id,
        department_id=identity.department_id,
        owner=identity.user_id,
        visibility=requested.visibility if requested else Visibility.internal,
        allowed_roles=list(requested.allowed_roles) if requested else [],
    )
    return await _client(request).ingest(req.model_copy(update={"acl": acl}))


@router.post("/upload", response_model=IngestResponse, summary="上传文件接入")
async def upload(
    request: Request,
    identity: DocumentWriter,
    file: Annotated[UploadFile, File()],
    visibility: Annotated[str, Form()] = "internal",
    reindex: Annotated[bool, Form()] = False,
) -> IngestResponse:
    data = await file.read()
    if not data:
        raise HTTPException(status_code=422, detail="上传文件为空")

    limit_bytes = _settings(request).max_upload_mb * 1024 * 1024
    if len(data) > limit_bytes:
        raise HTTPException(
            status_code=413, detail=f"文件超过 {_settings(request).max_upload_mb}MB 上限"
        )

    acl = ACL(
        tenant_id=identity.tenant_id,
        department_id=identity.department_id,
        visibility=Visibility(visibility),
        owner=identity.user_id,
    )
    return await _client(request).upload(
        file.filename or "upload.bin", data, acl=acl, reindex=reindex
    )


@router.get("", summary="文档列表（知识库台账）")
async def list_documents(
    request: Request,
    identity: DocumentReader,
    keyword: str = "",
    limit: int = 50,
    offset: int = 0,
) -> dict:
    # 只列本租户的文档：租户边界由身份决定，不接受客户端传 tenant_id
    return await _client(request).list_documents(
        tenant_id=identity.tenant_id,
        keyword=keyword or None,
        limit=min(max(limit, 1), 200),
        offset=max(offset, 0),
    )


@router.get("/jobs/{job_id}", summary="查询接入任务")
async def get_job(job_id: str, request: Request, identity: DocumentReader) -> dict:  # noqa: ARG001
    return await _client(request).get_job(job_id)


@router.get("/{doc_id}", summary="查询文档元数据")
async def get_document(doc_id: str, request: Request, identity: DocumentReader) -> dict:  # noqa: ARG001
    return await _client(request).get_document(doc_id)


@router.delete("/{doc_id}", summary="合规删除文档")
async def delete_document(doc_id: str, request: Request, identity: DocumentDeleter) -> dict:  # noqa: ARG001
    return await _client(request).delete_document(doc_id)
