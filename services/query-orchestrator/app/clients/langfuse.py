"""编排层的 Langfuse 追踪助手。

只上报**元数据与耗时**，chunk 原文统一经 ``truncate`` 截断，
避免把知识库正文外泄到第三方可观测平台。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from packages.common.logging import get_logger
from packages.observability import Tracer
from packages.observability.langfuse import truncate

logger = get_logger("orchestrator.langfuse")


def build_tracer(*, host: str, public_key: str, secret_key: str, service: str) -> Tracer:
    return Tracer(host=host, public_key=public_key, secret_key=secret_key, service=service)


@contextmanager
def trace_chat(
    tracer: Tracer,
    *,
    query: str,
    user_id: str,
    tenant_id: str,
    mode: str,
    top_k: int,
    trace_id: str | None = None,
) -> Iterator[Any]:
    with tracer.trace(
        "chat",
        user_id=user_id,
        tenant_id=tenant_id,
        mode=mode,
        top_k=top_k,
        query=truncate(query),
        trace_id=trace_id or "",
    ) as handle:
        yield handle


def record_stage(handle: Any, name: str, timings: dict[str, float], **meta: Any) -> None:
    """把某阶段耗时写进 trace（handle 为 no-op 时静默）。"""
    elapsed = timings.get(name)
    if elapsed is None:
        return
    handle.update(metadata={f"{name}_ms": elapsed, **meta})
