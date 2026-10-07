"""model-gateway 客户端（管理面只读查询）。"""

from __future__ import annotations

from typing import Any

from packages.common.http import ServiceClient
from packages.contracts import ModelInfo


class ModelGatewayClient:
    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self._client = ServiceClient(base_url, name="model-gateway", timeout=timeout)

    async def models(self) -> list[ModelInfo]:
        payload: list[dict[str, Any]] = await self._client.get("/models")
        return [ModelInfo.model_validate(item) for item in payload]

    async def quota(self, tenant_id: str) -> dict[str, Any]:
        return await self._client.get(f"/quota/{tenant_id}")

    async def ping(self) -> bool:
        return await self._client.ping()

    async def aclose(self) -> None:
        await self._client.aclose()
