"""对话路由：网关不参与 RAG 逻辑，只做鉴权与转发。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.clients.model_gateway import ModelGatewayClient
from app.clients.orchestrator import OrchestratorClient
from app.middleware.identity import require_action
from packages.contracts import ChatRequest, ChatResponse, ModelInfo
from packages.security import Identity

router = APIRouter(prefix="/chat", tags=["chat"])

# 依赖声明为模块级单例：既避免在参数默认值里调用函数，也让「这个路由需要什么权限」一眼可见
ChatCaller = Annotated[Identity, Depends(require_action("chat"))]


def _client(request: Request) -> OrchestratorClient:
    return request.app.state.orchestrator


@router.post("", response_model=ChatResponse, summary="知识库问答")
async def chat(req: ChatRequest, request: Request, identity: ChatCaller) -> ChatResponse:
    """POST /chat：鉴权（chat）后转发到编排层；网关不参与 RAG 逻辑。"""
    return await _client(request).chat(req, identity)


@router.get("/models", response_model=list[ModelInfo], summary="可选生成模型清单")
async def models(request: Request, identity: ChatCaller) -> list[ModelInfo]:  # noqa: ARG001
    """GET /chat/models：供聊天界面选择生成模型（与 /chat 同为 chat 权限）。

    刻意**不**复用 ``/admin/models``：挑模型是每个聊天用户的日常操作，
    挂在 admin 下等于普通用户永远看不到下拉内容（403 后前端只能静默降级成
    "只有一个模型"，用户无从判断是系统如此还是自己没权限）。

    返回里含 embedding 类模型，前端按 ``kind`` 过滤——让它选 embedding 模型没有意义。
    """
    model_gateway: ModelGatewayClient = request.app.state.model_gateway
    return await model_gateway.models()
