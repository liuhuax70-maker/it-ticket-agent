"""管理面路由：知识库统计、模型清单、租户配额。

开启鉴权后需要 ``rag_admin`` 角色；最小闭环（AUTHZ_ENABLED=false）下放行。
这些接口**全部只读**——管理面不提供绕过 API 权限体系的写入通道。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from app.clients.ingestion import IngestionClient
from app.clients.model_gateway import ModelGatewayClient
from app.middleware.identity import require_admin
from packages.contracts import ModelInfo
from packages.security import Identity

router = APIRouter(prefix="/admin", tags=["admin"])

AdminUser = Annotated[Identity, Depends(require_admin)]


@router.get("/stats", summary="知识库文档/分块统计")
async def stats(request: Request, identity: AdminUser) -> dict[str, Any]:  # noqa: ARG001
    """GET /admin/stats：知识库文档/分块统计（仅 admin）。"""
    ingestion: IngestionClient = request.app.state.ingestion
    return await ingestion.stats()


@router.get("/models", response_model=list[ModelInfo], summary="模型清单与当前生效模型")
async def models(request: Request, identity: AdminUser) -> list[ModelInfo]:  # noqa: ARG001
    """GET /admin/models：模型清单与当前生效模型（仅 admin）。"""
    model_gateway: ModelGatewayClient = request.app.state.model_gateway
    return await model_gateway.models()


@router.get("/quotas/{tenant_id}", summary="租户当日 token 用量")
async def quota(tenant_id: str, request: Request, identity: AdminUser) -> dict[str, Any]:  # noqa: ARG001
    """GET /admin/quotas/{tenant_id}：租户当日 token 用量（仅 admin）。"""
    model_gateway: ModelGatewayClient = request.app.state.model_gateway
    return await model_gateway.quota(tenant_id)


@router.get("/audit", summary="访问审计记录（最近的在前）")
async def audit(
    request: Request,
    identity: AdminUser,  # noqa: ARG001 - 仅作权限门槛
    tenant_id: str | None = None,
    user_id: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """审计查询。

    只有落库的记录能被查到（stdout 那份只用于实时 tail）；
    sink 未启用时返回空列表并注明原因，而不是 500——管理面保持可浏览。
    """
    sink = getattr(request.app.state, "audit_sink", None)
    if sink is None:
        return {"items": [], "note": "审计落库未启用（AUDIT_DATABASE_URL 为空或初始化失败）"}
    from sqlalchemy import select

    from packages.common.db import session_scope
    from packages.common.models import AuditLog

    limit = max(1, min(limit, 500))
    async with session_scope(request.app.state.settings.audit_database_url) as session:
        stmt = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
        if tenant_id:
            stmt = stmt.where(AuditLog.tenant_id == tenant_id)
        if user_id:
            stmt = stmt.where(AuditLog.user_id == user_id)
        rows = (await session.scalars(stmt)).all()
        items = [
            {
                "request_id": r.request_id,
                "method": r.method,
                "path": r.path,
                "status": r.status,
                "duration_ms": r.duration_ms,
                "tenant_id": r.tenant_id,
                "user_id": r.user_id,
                "client": r.client,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]
    return {"items": items, "count": len(items)}
