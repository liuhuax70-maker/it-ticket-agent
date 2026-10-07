"""model-gateway 服务客户端。"""

from __future__ import annotations

from packages.common.http import ServiceClient
from packages.contracts import CompletionRequest, GenerateRequest, GenerateResponse


class ModelGatewayClient:
    def __init__(self, base_url: str, timeout: float = 120.0) -> None:
        self._client = ServiceClient(base_url, name="model-gateway", timeout=timeout)

    async def generate(self, req: GenerateRequest) -> GenerateResponse:
        return await self._client.post("/generate", req, response_model=GenerateResponse)

    async def complete(self, req: CompletionRequest) -> GenerateResponse:
        """原始补全：用于查询改写与合规审核等自带 prompt 的场景。"""
        return await self._client.post("/complete", req, response_model=GenerateResponse)

    async def ping(self) -> bool:
        return await self._client.ping()

    async def aclose(self) -> None:
        await self._client.aclose()
