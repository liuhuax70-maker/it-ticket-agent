"""降级策略：决定「什么错误值得重试、重试几次、间隔多久」。"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from packages.common.errors import ConfigError, UpstreamError

# 可重试的错误类型：网络抖动与上游 5xx/限流都算；
# 配置错误（缺 key / provider 名写错）不可重试，重试只会浪费配额。
RETRYABLE: tuple[type[BaseException], ...] = (
    UpstreamError,
    httpx.HTTPError,
    TimeoutError,
    ConnectionError,
)


@dataclass(frozen=True)
class FallbackPolicy:
    """兜底策略：最大尝试次数（主 + 兜底模型数）与冷却间隔。

    ``is_retryable`` 区分可重试（网络抖动 / 上游 5xx / 限流）与不可重试（配置错误）——后者
    重试只会浪费配额。
    """

    max_attempts: int = 1
    cooldown_seconds: float = 0.5

    @classmethod
    def from_settings(cls, fallback_count: int, cooldown_seconds: float) -> FallbackPolicy:
        return cls(max_attempts=max(1, 1 + fallback_count), cooldown_seconds=cooldown_seconds)

    def is_retryable(self, exc: BaseException) -> bool:
        if isinstance(exc, ConfigError):
            return False
        return isinstance(exc, RETRYABLE)
