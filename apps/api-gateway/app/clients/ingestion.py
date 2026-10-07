"""ingestion 客户端（接入 / 查询 / 合规删除）。"""

from __future__ import annotations

from typing import Any

from packages.common.http import ServiceClient
from packages.contracts import ACL, IngestRequest, IngestResponse


class IngestionClient:
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
                "reindex": str(reindex).lower(),
            },
            response_model=IngestResponse,
        )

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
