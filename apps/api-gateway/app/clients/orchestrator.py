"""query-orchestrator 客户端。

身份以请求头形式下传（``X-Tenant-Id`` 等）：网关是唯一鉴权点，
下游服务信任这些头（部署上要求下游不可从公网直达）。
"""

from __future__ import annotations

from packages.common.http import ServiceClient
from packages.contracts import ChatRequest, ChatResponse
from packages.security import Identity


def identity_headers(identity: Identity) -> dict[str, str]:
    """把身份压平为请求头下传给 query-orchestrator。

    网关是唯一鉴权点，下游无条件信任这些头；因此 tenant/department/roles
    必须完整且来自已校验的 Identity，绝不可由客户端透传掺假。
    """
    return {
        "x-user-id": identity.user_id,
        "x-tenant-id": identity.tenant_id,
        "x-department-id": identity.department_id,
        "x-user-roles": ",".join(identity.roles),
    }


class OrchestratorClient:
    """query-orchestrator 的 HTTP 客户端。

    仅转发已鉴权请求并下传身份头，不缓存、不改写任何决策结果；
    真正的检索编排与权限下推发生在下游服务。
    """

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
