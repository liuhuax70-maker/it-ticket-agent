"""服务间 HTTP 客户端。

最小闭环采用**同步直连**（服务间同步 HTTP 调用），而非 Kafka 异步：
本模块是所有跨服务调用的唯一出口，后续切异步只需替换调用点。
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
    """面向单个下游服务的异步客户端，自带重试与统一错误包装。"""

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
        response_model: type[M] | None = None,
        timeout: float | None = None,
    ) -> Any:
        url = path if path.startswith("/") else f"/{path}"
        try:
            resp = await self._client.request(method, url, json=json_body, timeout=timeout)
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
        response_model: type[M] | None = None,
        timeout: float | None = None,
    ) -> Any:
        return await self._request("GET", path, response_model=response_model, timeout=timeout)

    async def post(
        self,
        path: str,
        payload: BaseModel | dict[str, Any] | None = None,
        *,
        response_model: type[M] | None = None,
        timeout: float | None = None,
    ) -> Any:
        if isinstance(payload, BaseModel):
            body = payload.model_dump(mode="json")
        else:
            body = payload
        return await self._request(
            "POST", path, json_body=body, response_model=response_model, timeout=timeout
        )

    async def ping(self, path: str = "/health") -> bool:
        try:
            await self._client.get(path, timeout=3.0)
            return True
        except httpx.HTTPError:
            return False
