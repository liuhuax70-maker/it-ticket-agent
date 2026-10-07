"""Langfuse trace 包装。

三项原则：
    * 未配置 host/keys 时**完全静默**，不产生任何网络开销；
    * SDK 未安装时降级为 no-op，不阻塞业务；
    * 上报内容必须截断（``truncate``），**禁止上报 chunk 原文全文**。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from packages.common.logging import get_logger

logger = get_logger("observability.langfuse")

MAX_TEXT = 200


def truncate(text: str, limit: int = MAX_TEXT) -> str:
    if text is None:
        return ""
    return text if len(text) <= limit else text[:limit] + "..."


class _NoopHandle:
    """无 Langfuse 时的占位句柄，接口与真实句柄一致。"""

    def update(self, **_: Any) -> None:
        return None

    @contextmanager
    def span(self, name: str, **meta: Any) -> Iterator[_NoopHandle]:  # noqa: ARG002
        yield self

    def end(self) -> None:
        return None


class _TraceHandle:
    def __init__(self, raw: Any) -> None:
        self._raw = raw

    def update(self, **kwargs: Any) -> None:
        try:
            self._raw.update(**kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.debug("langfuse trace.update 失败（忽略）: %s", exc)

    @contextmanager
    def span(self, name: str, **meta: Any) -> Iterator[_TraceHandle]:
        span = None
        try:
            span = self._raw.span(name=name, metadata=meta)
        except Exception as exc:  # noqa: BLE001
            logger.debug("langfuse span 创建失败（忽略）: %s", exc)
        handle = _TraceHandle(span) if span is not None else _NoopHandle()
        try:
            yield handle
        finally:
            if span is not None:
                try:
                    span.end()
                except Exception:  # noqa: BLE001
                    pass

    def end(self) -> None:
        return None


class Tracer:
    """对外唯一入口；``enabled=False`` 时所有方法都是 no-op。"""

    def __init__(
        self,
        host: str = "",
        public_key: str = "",
        secret_key: str = "",
        service: str = "unknown",
    ) -> None:
        self.service = service
        self._client: Any = None
        self.enabled = False

        if not (host and public_key and secret_key):
            logger.debug("Langfuse 未配置，追踪关闭")
            return
        try:
            from langfuse import Langfuse  # 可选依赖
        except ImportError:
            logger.warning(
                "未安装 langfuse SDK，追踪降级为 no-op：pip install -e '.[observability]'"
            )
            return
        try:
            self._client = Langfuse(host=host, public_key=public_key, secret_key=secret_key)
            self.enabled = True
            logger.info("Langfuse 追踪已启用: %s", host)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Langfuse 初始化失败，降级为 no-op: %s", exc)

    @contextmanager
    def trace(self, name: str, **metadata: Any) -> Iterator[_TraceHandle | _NoopHandle]:
        if not self.enabled or self._client is None:
            yield _NoopHandle()
            return
        raw = None
        try:
            raw = self._client.trace(name=name, metadata={"service": self.service, **metadata})
        except Exception as exc:  # noqa: BLE001
            logger.debug("langfuse trace 创建失败（忽略）: %s", exc)
        try:
            yield _TraceHandle(raw) if raw is not None else _NoopHandle()
        finally:
            self.flush()

    def flush(self) -> None:
        if self._client is not None:
            try:
                self._client.flush()
            except Exception:  # noqa: BLE001
                pass
