"""feedback 服务客户端。

最小闭环里 feedback 服务未接入闭环（见 docs/architecture 中的阶段说明），
客户端先就位：网关的 /feedback 会如实返回下游不可用（503），
而不是假装写入成功——静默丢反馈比接口报错更糟。
"""

from __future__ import annotations

from packages.common.http import ServiceClient
from packages.contracts import FeedbackRequest, FeedbackResponse


class FeedbackClient:
    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self._client = ServiceClient(base_url, name="feedback", timeout=timeout)

    async def submit(self, req: FeedbackRequest) -> FeedbackResponse:
        return await self._client.post("/feedback", req, response_model=FeedbackResponse)

    async def aclose(self) -> None:
        await self._client.aclose()
