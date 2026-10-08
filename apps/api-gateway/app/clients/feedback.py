"""feedback 服务客户端。

最小闭环里 feedback 服务未接入闭环（见 docs/architecture 中的阶段说明），
客户端先就位：网关的 /feedback 会如实返回下游不可用（503），
而不是假装写入成功——静默丢反馈比接口报错更糟。
"""

from __future__ import annotations

from packages.common.http import ServiceClient
from packages.contracts import FeedbackRequest, FeedbackResponse


class FeedbackClient:
    """feedback 服务的 HTTP 客户端。

    下游不可用时直接抛出 503（由 ServiceClient 传播），网关不做重试或
    静默丢弃——丢反馈比明明白白报错更难排查。
    """

    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self._client = ServiceClient(base_url, name="feedback", timeout=timeout)

    async def submit(self, req: FeedbackRequest) -> FeedbackResponse:
        return await self._client.post("/feedback", req, response_model=FeedbackResponse)

    async def aclose(self) -> None:
        await self._client.aclose()
