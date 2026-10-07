"""反馈路由。

网关只做两件事：补全身份字段、转发。反馈落到哪个存储由 feedback 服务决定，
网关不直接写库——否则「反馈」会出现两个写入方。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from packages.contracts import FeedbackRequest, FeedbackResponse
from packages.security import Identity

from app.clients.feedback import FeedbackClient
from app.middleware.identity import require_action

router = APIRouter(prefix="/feedback", tags=["feedback"])


@router.post("", response_model=FeedbackResponse, summary="提交问答反馈")
async def submit(
    req: FeedbackRequest,
    request: Request,
    identity: Identity = Depends(require_action("feedback:write")),
) -> FeedbackResponse:
    client: FeedbackClient = request.app.state.feedback
    return await client.submit(req)
