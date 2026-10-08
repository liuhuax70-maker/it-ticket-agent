"""节点公共工具：耗时累计。

每个节点只负责「把自己的耗时并进 state.timings」，
这样 /chat 的 timings_ms 是各节点自报之和，而不是外层拍脑袋估的。
"""

from __future__ import annotations

import time
from typing import Any

from app.graph.state import RAGState


def ms(started: float) -> float:
    """毫秒计时：``perf_counter`` 差值转毫秒并保留 1 位小数。"""
    return round((time.perf_counter() - started) * 1000, 1)


def merge_timing(state: RAGState, name: str, started: float, **extra: Any) -> dict[str, Any]:
    """把节点耗时并入 ``state.timings``，并透传额外字段（如 hits/answer/citations）。"""
    timings = dict(state.get("timings") or {})
    timings[name] = ms(started)
    return {"timings": timings, **extra}


def add_error(state: RAGState, message: str) -> list[str]:
    """向 ``state.errors`` 追加一条错误标记（返回新列表，不就地修改 state）。"""
    errors = list(state.get("errors") or [])
    errors.append(message)
    return errors
