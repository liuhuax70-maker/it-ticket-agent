"""query-orchestrator 客户端。

身份以请求头形式下传（``X-Tenant-Id`` 等）：网关是唯一鉴权点，
下游服务信任这些头（部署上要求下游不可从公网直达）。
"""

from __future__ import annotations

from packages.common.http import ServiceClient
from packages.contracts import ChatRequest, ChatResponse
from packages.security import Identity


def identity_headers(identity: Identity) -> dict[str, str]:
    return {
        "x-user-id": identity.user_id,
        "x-tenant-id": identity.tenant_id,
        "x-department-id": identity.department_id,
        "x-user-roles": ",".join(identity.roles),
    }


class OrchestratorClient:
    def __init__(self, base_url: str, timeout: float = 180.0) -> None:
        self._client = ServiceClient(base_url, name="query-orchestrator", timeout=timeout)

    async def chat(self, req: ChatRequest, identity: Identity) -> ChatResponse:
        return await self._client.post(
            "/chat",
            req,
            headers=identity_headers(identity),
            response_model=ChatResponse,
        )

    async def ping(self) -> bool:
        return await self._client.ping()

    async def aclose(self) -> None:
        await self._client.aclose()
