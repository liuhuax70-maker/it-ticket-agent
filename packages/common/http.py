"""服务间 HTTP 客户端。

最小闭环采用**同步直连**（服务间同步 HTTP 调用），而非 Kafka 异步。

范围说明：本模块是**绝大多数**跨服务调用的统一出口（超时、错误包装、契约校验），
但不是唯一出口——``services/eval/app/collector.py``、``services/authz``、
``services/model-gateway/app/fallback.py``、``pipelines/ingestion_dag/sync.py``
直接使用 httpx。因此任何"统一超时/统一错误/统一脱敏"的假设都要按调用点核对，
不能假定全仓都走这里。
"""

from __future__ import annotations

from typing import Any, TypeVar

import httpx
from pydantic import BaseModel

from packages.common.errors import UpstreamError
from packages.common.logging import get_logger

logger = get_logger("common.http")

M = TypeVar("M", bound=BaseModel)


class ServiceClient:
    """面向单个下游服务的异步客户端，统一超时与错误包装。

    关于 ``retries``：它只作用于 httpx 的**连接层**错误（连接被拒、连接中断），
    **不会**对 5xx / 429 重试，也没有退避。因此：

        * 它对"下游在处理中途断开"有效；
        * 它对"下游返回 503"无效——那需要业务层显式重试（见 model-gateway 的 fallback）；
        * 超时后重试要求目标操作幂等。本仓库里 ``/index``（按 chunk_id upsert）、
          ``/documents/{id}``（先删后建）满足幂等；但 ``/ingest/upload``、
          ``/feedback`` 这类"追加"语义的操作在超时后重试可能产生重复记录，
          接入前需自行确认。
    """

    def __init__(
        self,
        base_url: str,
        *,
        name: str = "upstream",
        timeout: float = 60.0,
        retries: int = 2,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout),
            transport=httpx.AsyncHTTPTransport(retries=retries),
            headers={"user-agent": "permission-aware-rag/0.1.0"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        files: list[tuple[str, tuple[str, bytes, str]]] | None = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        response_model: type[M] | None = None,
        timeout: float | None = None,
    ) -> Any:
        url = path if path.startswith("/") else f"/{path}"
        try:
            resp = await self._client.request(
                method,
                url,
                params=params,
                json=json_body,
                files=files,
                data=data,
                headers=headers,
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            raise UpstreamError(self.name, f"连接失败: {exc}") from exc

        if resp.status_code >= 400:
            body = resp.text[:500]
            raise UpstreamError(
                self.name,
                f"{method} {url} 返回 {resp.status_code}",
                detail={"body": body},
            )

        if response_model is None:
            return resp.json() if resp.content else None
        try:
            return response_model.model_validate(resp.json())
        except Exception as exc:  # noqa: BLE001 - 契约不匹配属于严重问题，需显式暴露
            raise UpstreamError(
                self.name,
                f"{method} {url} 响应不符合契约: {exc}",
                detail={"body": resp.text[:500]},
            ) from exc

    async def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        response_model: type[M] | None = None,
        timeout: float | None = None,
    ) -> Any:
        return await self._request(
            "GET",
            path,
            params=params,
            headers=headers,
            response_model=response_model,
            timeout=timeout,
        )

    async def post(
        self,
        path: str,
        payload: BaseModel | dict[str, Any] | None = None,
        *,
        headers: dict[str, str] | None = None,
        response_model: type[M] | None = None,
        timeout: float | None = None,
    ) -> Any:
        if isinstance(payload, BaseModel):
            body = payload.model_dump(mode="json")
        else:
            body = payload
        return await self._request(
            "POST",
            path,
            json_body=body,
            headers=headers,
            response_model=response_model,
            timeout=timeout,
        )

    async def delete(
        self,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        response_model: type[M] | None = None,
        timeout: float | None = None,
    ) -> Any:
        return await self._request(
            "DELETE", path, headers=headers, response_model=response_model, timeout=timeout
        )

    async def post_file(
        self,
        path: str,
        *,
        filename: str,
        content: bytes,
        fields: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        response_model: type[M] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """multipart/form-data 上传。"""
        files = [("file", (filename, content, "application/octet-stream"))]
        return await self._request(
            "POST",
            path,
            files=files,
            data={k: str(v) for k, v in (fields or {}).items()},
            headers=headers,
            response_model=response_model,
            timeout=timeout,
        )

    async def ping(self, path: str = "/health") -> bool:
        try:
            await self._client.get(path, timeout=3.0)
            return True
        except httpx.HTTPError:
            return False
