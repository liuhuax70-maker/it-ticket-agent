"""检索调试接口。

契约见 `开发流程/05-接口与数据契约设计.md` §4.4。
仅用于内部调试：直接跑一遍混合检索并回传各路命中情况。
"""

import asyncio

from fastapi import APIRouter, Request

from app.retrieval.hybrid import hybrid_search
from app.schemas.common import ApiResponse
from app.schemas.retrieval import SearchRequest, SearchResponse

router = APIRouter(prefix="/retrieval", tags=["retrieval"])


@router.post("/search", response_model=ApiResponse[SearchResponse], summary="检索调试")
async def search(payload: SearchRequest, request: Request) -> ApiResponse[SearchResponse]:
    # hybrid_search 是同步且较慢（含 embedding），放线程池避免阻塞事件循环
    chunks, mode, debug = await asyncio.to_thread(
        hybrid_search,
        payload.query,
        payload.top_k,
        payload.top_n_dense,
        payload.top_n_sparse,
        payload.top_n_fused,
        payload.rerank_enabled,
        payload.filters,
    )

    data = SearchResponse(
        mode=mode,
        dense_hits=debug.get("dense_hits", 0),
        sparse_hits=debug.get("sparse_hits", 0),
        fused=debug.get("fused", 0),
        reranked=debug.get("reranked", False),
        chunks=chunks,
    )
    return ApiResponse(
        code=0,
        message="ok",
        trace_id=getattr(request.state, "trace_id", None),
        data=data,
    )
