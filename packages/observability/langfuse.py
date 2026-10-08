"""Langfuse trace 包装。

三项原则：
    * 未配置 host/keys 时**完全静默**，不产生任何网络开销；
    * SDK 未安装时降级为 no-op，不阻塞业务；
    * 上报内容必须截断（``truncate``），**禁止上报 chunk 原文全文**。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any, Protocol

from packages.common.logging import get_logger

logger = get_logger("observability.langfuse")

MAX_TEXT = 200


def truncate(text: str, limit: int = MAX_TEXT) -> str:
    """截断到 ``limit`` 字符（默认 200），避免把 chunk 原文等超长内容上报到 Langfuse。"""
    if text is None:
        return ""
    return text if len(text) <= limit else text[:limit] + "..."


class _SpanHandle(Protocol):
    """span 句柄的公共接口。

    真实句柄与 noop 句柄结构平行但 __init__ 不同（后者不需要 raw span），
    因此用 Protocol 统一类型——调用方拿到的无论是哪种实现，
    都能按同一套接口 update/span/end，不需要先判断是哪一种。
    """

    def update(self, **kwargs: Any) -> None: ...

    def span(self, name: str, **meta: Any) -> AbstractContextManager[_SpanHandle]: ...

    def end(self) -> None: ...


class _NoopHandle:
    """无 Langfuse 时的占位句柄，接口与真实句柄一致。"""

    def update(self, **_: Any) -> None:
        """no-op：未启用追踪时什么都不做。"""
        return None

    @contextmanager
    def span(self, name: str, **meta: Any) -> Iterator[_SpanHandle]:  # noqa: ARG002
        """no-op：直接让出自身，调用方写法与真实句柄完全相同。"""
        yield self

    def end(self) -> None:
        """no-op。"""
        return None


class _TraceHandle:
    """真实句柄：包装一个 Langfuse raw span，任何失败都静默忽略（观测不能影响业务）。"""

    def __init__(self, raw: Any) -> None:
        self._raw = raw

    def update(self, **kwargs: Any) -> None:
        """更新 raw span 元数据；失败静默忽略。"""
        try:
            self._raw.update(**kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.debug("langfuse trace.update 失败（忽略）: %s", exc)

    @contextmanager
    def span(self, name: str, **meta: Any) -> Iterator[_SpanHandle]:
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
        """no-op：真实 span 已在 ``span()`` 的 finally 中 end。"""
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
        """按 host/public_key/secret_key 初始化；任一缺失或 SDK 未装则降级 no-op（不阻塞业务）。"""
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
    def trace(self, name: str, **metadata: Any) -> Iterator[_SpanHandle]:
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
        """刷新待上报事件；忽略所有异常（flush 失败不应影响业务请求）。"""
        if self._client is not None:
            try:
                self._client.flush()
            except Exception:  # noqa: BLE001
                pass
