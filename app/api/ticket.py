"""工单接口（骨架占位）。

契约见 `开发流程/05-接口与数据契约设计.md` §4.1 / §4.2：
- POST /ticket/query   提交工单，SSE 流式返回
- POST /ticket/review  提交人工审核结果，恢复编排流程
"""

from fastapi import APIRouter, HTTPException

from app.schemas.ticket import ReviewRequest, TicketQueryRequest

router = APIRouter(prefix="/ticket", tags=["ticket"])

_NOT_IMPLEMENTED = "骨架占位：该接口将在后续编码阶段实现"


@router.post("/query", summary="提交工单（SSE 流式）")
async def query_ticket(payload: TicketQueryRequest) -> None:
    # TODO(后续)：接入 LangGraph 编排，以 text/event-stream 推送
    #   start / intent / retrieval / token / draft / review_required / done / error
    raise HTTPException(status_code=501, detail=_NOT_IMPLEMENTED)


@router.post("/review", summary="提交人工审核结果")
async def review_ticket(payload: ReviewRequest) -> None:
    # TODO(后续)：以 Command(resume=...) 恢复被 interrupt 挂起的图
    raise HTTPException(status_code=501, detail=_NOT_IMPLEMENTED)
