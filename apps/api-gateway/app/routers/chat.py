"""对话路由：网关不参与 RAG 逻辑，只做鉴权与转发。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from packages.contracts import ChatRequest, ChatResponse
from packages.security import Identity

from app.clients.orchestrator import OrchestratorClient
from app.middleware.identity import require_action

router = APIRouter(prefix="/chat", tags=["chat"])


def _client(request: Request) -> OrchestratorClient:
    return request.app.state.orchestrator


@router.post("", response_model=ChatResponse, summary="知识库问答")
async def chat(
    req: ChatRequest,
    request: Request,
    identity: Identity = Depends(require_action("chat")),
) -> ChatResponse:
    return await _client(request).chat(req, identity)
