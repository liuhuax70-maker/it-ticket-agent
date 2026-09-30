"""会话查询接口（骨架占位）。

契约见 `开发流程/05-接口与数据契约设计.md` §4.3。
"""

from fastapi import APIRouter, HTTPException

from app.schemas.common import ApiResponse

router = APIRouter(prefix="/session", tags=["session"])


@router.get("/{session_id}", response_model=ApiResponse[dict], summary="查询会话状态")
async def get_session(session_id: str) -> ApiResponse[dict]:
    # TODO(后续)：从 Checkpointer / 会话存储读取该会话的工单列表
    raise HTTPException(status_code=501, detail="骨架占位：该接口将在后续编码阶段实现")
