"""管理面路由：知识库统计、模型清单、租户配额。

开启鉴权后需要 ``rag_admin`` 角色；最小闭环（AUTHZ_ENABLED=false）下放行。
这些接口**全部只读**——管理面不提供绕过 API 权限体系的写入通道。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from packages.contracts import ModelInfo
from packages.security import Identity

from app.clients.ingestion import IngestionClient
from app.clients.model_gateway import ModelGatewayClient
from app.middleware.identity import require_admin

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/stats", summary="知识库文档/分块统计")
async def stats(
    request: Request,
    identity: Identity = Depends(require_admin),  # noqa: ARG001
) -> dict[str, Any]:
    ingestion: IngestionClient = request.app.state.ingestion
    return await ingestion.stats()


@router.get("/models", response_model=list[ModelInfo], summary="模型清单与当前生效模型")
async def models(
    request: Request,
    identity: Identity = Depends(require_admin),  # noqa: ARG001
) -> list[ModelInfo]:
    model_gateway: ModelGatewayClient = request.app.state.model_gateway
    return await model_gateway.models()


@router.get("/quotas/{tenant_id}", summary="租户当日 token 用量")
async def quota(
    tenant_id: str,
    request: Request,
    identity: Identity = Depends(require_admin),  # noqa: ARG001
) -> dict[str, Any]:
    model_gateway: ModelGatewayClient = request.app.state.model_gateway
    return await model_gateway.quota(tenant_id)
