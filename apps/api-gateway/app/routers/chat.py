"""对话路由：网关不参与 RAG 逻辑，只做鉴权与转发。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.clients.orchestrator import OrchestratorClient
from app.middleware.identity import require_action
from packages.contracts import ChatRequest, ChatResponse
from packages.security import Identity

router = APIRouter(prefix="/chat", tags=["chat"])

# 依赖声明为模块级单例：既避免在参数默认值里调用函数，也让「这个路由需要什么权限」一眼可见
ChatCaller = Annotated[Identity, Depends(require_action("chat"))]


def _client(request: Request) -> OrchestratorClient:
    return request.app.state.orchestrator


@router.post("", response_model=ChatResponse, summary="知识库问答")
async def chat(req: ChatRequest, request: Request, identity: ChatCaller) -> ChatResponse:
    return await _client(request).chat(req, identity)
