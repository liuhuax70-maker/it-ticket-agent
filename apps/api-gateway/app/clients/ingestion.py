"""ingestion 客户端（接入 / 查询 / 合规删除）。"""

from __future__ import annotations

from typing import Any

from packages.common.http import ServiceClient
from packages.contracts import ACL, IngestRequest, IngestResponse


class IngestionClient:
    """接入/查询/合规删除的 HTTP 客户端。

    注意 ACL 由上游（网关路由层）按身份强制写入再下传，本客户端只负责
    原样投递，不做二次校验——校验权集中在网关，避免两端逻辑漂移。
    """

    def __init__(self, base_url: str, timeout: float = 600.0) -> None:
        self._client = ServiceClient(base_url, name="ingestion", timeout=timeout)

    async def ingest(self, req: IngestRequest) -> IngestResponse:
        return await self._client.post("/ingest", req, response_model=IngestResponse)

    async def upload(
        self, filename: str, content: bytes, *, acl: ACL, reindex: bool = False
    ) -> IngestResponse:
        return await self._client.post_file(
            "/ingest/upload",
            filename=filename,
            content=content,
            fields={
                "tenant_id": acl.tenant_id,
                "department_id": acl.department_id,
                "visibility": acl.visibility.value,
                # owner 必须带上：漏了它会让 private 可见性静默退化为「谁都不看不到」
                "owner": acl.owner or "",
                "reindex": str(reindex).lower(),
            },
            response_model=IngestResponse,
        )

    async def list_documents(
        self,
        *,
        tenant_id: str | None = None,
        keyword: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        # 交给 httpx 拼装与编码：关键字可能是中文，手工拼串容易踩编码坑
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if tenant_id:
            params["tenant_id"] = tenant_id
        if keyword:
            params["keyword"] = keyword
        return await self._client.get("/documents", params=params)

    async def get_document(self, doc_id: str) -> dict[str, Any]:
        return await self._client.get(f"/documents/{doc_id}")

    async def delete_document(self, doc_id: str) -> dict[str, Any]:
        return await self._client.delete(f"/documents/{doc_id}")

    async def get_job(self, job_id: str) -> dict[str, Any]:
        return await self._client.get(f"/jobs/{job_id}")

    async def stats(self) -> dict[str, Any]:
        return await self._client.get("/stats")

    async def ping(self) -> bool:
        return await self._client.ping()

    async def aclose(self) -> None:
        await self._client.aclose()
