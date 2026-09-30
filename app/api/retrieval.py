"""检索调试接口（骨架占位）。

契约见 `开发流程/05-接口与数据契约设计.md` §4.4。
仅用于内部调试，生产环境可关闭或加鉴权。
"""

from fastapi import APIRouter, HTTPException

from app.schemas.common import ApiResponse
from app.schemas.retrieval import SearchRequest, SearchResponse

router = APIRouter(prefix="/retrieval", tags=["retrieval"])


@router.post("/search", response_model=ApiResponse[SearchResponse], summary="检索调试")
async def search(payload: SearchRequest) -> ApiResponse[SearchResponse]:
    # TODO(后续)：调用 app.retrieval.hybrid.hybrid_search 并返回各路命中情况
    raise HTTPException(status_code=501, detail="骨架占位：该接口将在后续编码阶段实现")
