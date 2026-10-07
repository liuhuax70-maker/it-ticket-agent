"""反馈路由。

网关只做一件事：**鉴权**（``feedback:write``）与转发。反馈落到哪个存储由 feedback
服务决定，网关不直接写库——否则「反馈」会出现两个写入方。

⚠️ 当前**没有**把身份注入给下游：`FeedbackRequest` 契约里没有 user_id/tenant_id，
客户端也没带身份头。feedback 服务因此只能靠请求头兜底
（``x-tenant-id`` / ``x-user-id``，缺失时分别落成 ``"default"`` 与 ``""``）。
后果是**按租户统计、按用户追责、坏例导出都会失真**，且不会报错。
所以现状下反馈只能按 ``trace_id`` 关联问答，不能按人归集；
若要按人归集，必须先给契约加身份字段并在客户端补 ``identity_headers()``
（参照 ``clients/orchestrator.py``）。改动前请同步更新契约与 feedback 服务。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.clients.feedback import FeedbackClient
from app.middleware.identity import require_action
from packages.contracts import FeedbackRequest, FeedbackResponse
from packages.security import Identity

router = APIRouter(prefix="/feedback", tags=["feedback"])

FeedbackWriter = Annotated[Identity, Depends(require_action("feedback:write"))]


@router.post("", response_model=FeedbackResponse, summary="提交问答反馈")
async def submit(
    req: FeedbackRequest, request: Request, identity: FeedbackWriter
) -> FeedbackResponse:
    # identity 目前只用于鉴权（依赖注入即校验），未下传——见模块 docstring 的说明。
    del identity
    client: FeedbackClient = request.app.state.feedback
    return await client.submit(req)
