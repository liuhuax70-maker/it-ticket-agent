"""工单接口。

契约见 `开发流程/05-接口与数据契约设计.md` §4.1 / §4.2：
- POST /ticket/query   提交工单，SSE 流式返回
- POST /ticket/review  提交人工审核结果，恢复编排流程
"""

import logging
from typing import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from langgraph.types import Command

from app.api.sse import SSEWriter, SSE_HEADERS
from app.core.errors import AppError, ErrorCode
from app.graph.build import get_graph
from app.memory.checkpointer import streaming_thread_config, thread_config
from app.schemas.common import ApiResponse
from app.schemas.ticket import ReviewRequest, TicketQueryRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ticket", tags=["ticket"])


async def _event_stream(state: dict, config: dict) -> AsyncIterator[str]:
    """把图的流式输出翻译成 SSE 事件。

    节点更新 → 业务事件（intent / retrieval / draft）；
    草稿节点的自定义写入 → token 事件；
    图中断 → review_required，随后结束流（等待 /ticket/review）。
    """
    graph = await get_graph()
    sse = SSEWriter()

    yield sse.event(
        "start", {"ticket_id": state.get("ticket_id"), "session_id": state.get("session_id")}
    )

    try:
        async for mode, chunk in graph.astream(state, config, stream_mode=["updates", "custom"]):
            if mode == "custom":
                # 草稿节点推送的 {"event": "token", "delta": ...}
                event = chunk.get("event", "token")
                yield sse.event(event, {k: v for k, v in chunk.items() if k != "event"})
                continue

            if "__interrupt__" in chunk:
                interrupts = chunk["__interrupt__"]
                payload = interrupts[0].value if interrupts else {}
                yield sse.event(
                    "review_required",
                    {"ticket_id": payload.get("ticket_id"), "reason": payload.get("reason")},
                )
                return  # 流到此结束，等待人工审核

            for node_name, node_output in chunk.items():
                if node_name == "intent":
                    yield sse.event(
                        "intent",
                        {
                            "intent": node_output.get("intent"),
                            "need_review": node_output.get("need_review"),
                        },
                    )
                elif node_name == "retrieve":
                    debug = node_output.get("retrieval_debug") or {}
                    retrieved = node_output.get("retrieved") or []
                    yield sse.event(
                        "retrieval",
                        {
                            "mode": debug.get("mode"),
                            "hit_count": len(retrieved),
                            "top_docs": [
                                {"chunk_id": c.get("chunk_id"), "title": c.get("title")}
                                for c in retrieved
                            ],
                        },
                    )
                elif node_name == "draft":
                    yield sse.event(
                        "draft",
                        {
                            "draft": node_output.get("draft", ""),
                            "citations": node_output.get("citations") or [],
                        },
                    )

        snapshot = await graph.aget_state(config)
        values = snapshot.values or {}
        yield sse.event(
            "done",
            {
                "reply": values.get("reply"),
                "send_status": values.get("send_status"),
                "citations": values.get("citations") or [],
            },
        )
    except Exception as exc:  # noqa: BLE001 - 任何异常都以 error 事件收尾，客户端需能感知
        logger.exception("工单处理失败")
        code = exc.code if isinstance(exc, AppError) else ErrorCode.UNKNOWN
        yield sse.event("error", {"code": int(code), "message": str(exc)})


@router.post("/query", summary="提交工单（SSE 流式）")
async def query_ticket(payload: TicketQueryRequest, request: Request) -> StreamingResponse:
    graph = await get_graph()
    config = thread_config(payload.session_id)

    snapshot = await graph.aget_state(config)
    if snapshot.next:
        # 该会话有工单正卡在人工审核，必须先审核再提交新问题
        raise AppError(
            ErrorCode.CONFLICT,
            f"会话 {payload.session_id} 有工单正在等待人工审核，请先提交审核结果",
        )

    state = {
        "session_id": payload.session_id,
        "ticket_id": payload.ticket_id,
        "query": payload.query,
        "history": [message.model_dump(mode="json") for message in payload.history],
    }

    return StreamingResponse(
        _event_stream(state, streaming_thread_config(payload.session_id)),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.post("/review", response_model=ApiResponse[dict], summary="提交人工审核结果")
async def review_ticket(payload: ReviewRequest, request: Request) -> ApiResponse[dict]:
    graph = await get_graph()
    config = thread_config(payload.session_id)

    snapshot = await graph.aget_state(config)
    if not snapshot.next:
        raise AppError(ErrorCode.CONFLICT, "该会话当前没有等待审核的工单")

    pending_ticket = (snapshot.values or {}).get("ticket_id")
    if pending_ticket != payload.ticket_id:
        raise AppError(
            ErrorCode.CONFLICT,
            f"待审核工单为 {pending_ticket}，与提交的 {payload.ticket_id} 不一致",
        )

    # 只把有值的字段传给图，避免覆盖状态里的既有内容
    resume_payload = payload.model_dump(mode="json", exclude={"session_id"}, exclude_none=True)
    result = await graph.ainvoke(Command(resume=resume_payload), config)

    return ApiResponse(
        code=0,
        message="ok",
        trace_id=getattr(request.state, "trace_id", None),
        data={
            "ticket_id": result.get("ticket_id"),
            "review_status": result.get("review_status"),
            "send_status": result.get("send_status"),
            "reply": result.get("reply"),
        },
    )
