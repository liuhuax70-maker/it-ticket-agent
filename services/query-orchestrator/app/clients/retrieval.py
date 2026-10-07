"""retrieval 服务客户端。"""

from __future__ import annotations

from packages.common.http import ServiceClient
from packages.contracts import (
    RerankRequest,
    RerankResponse,
    SearchRequest,
    SearchResponse,
)


class RetrievalClient:
    def __init__(self, base_url: str, timeout: float = 60.0) -> None:
        self._client = ServiceClient(base_url, name="retrieval", timeout=timeout)

    async def search(self, req: SearchRequest) -> SearchResponse:
        return await self._client.post("/search", req, response_model=SearchResponse)

    async def rerank(self, req: RerankRequest) -> RerankResponse:
        return await self._client.post("/rerank", req, response_model=RerankResponse)

    async def ping(self) -> bool:
        return await self._client.ping()

    async def aclose(self) -> None:
        await self._client.aclose()
