"""后台任务的统一生命周期管理。

两个真实踩过的坑（USE_KAFKA=true 时）：
    1. 消费任务在启动时抛错（broker 不可达 / aiokafka 未安装）会**立刻死亡**，
       异常挂在 task 上无人 retrieve——服务 health 正常、无任何告警，
       整条异步通道静默失效。supervise() 用 done-callback 把意外死亡变成
       一条 error 日志，可据此配置告警。
    2. 关闭时只 cancel 不 await：正在处理中的消息被硬切断（配合 Kafka 的
       预提交语义 = 该消息既没处理完也不会重投），还会打出
       "Task was destroyed but it is pending"。
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from contextlib import suppress
from typing import Any

from packages.common.logging import get_logger

logger = get_logger("common.background")


def spawn_supervised(coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task:
    """创建带死亡告警的后台任务。"""
    task = asyncio.create_task(coro, name=name)

    def _done(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logger.error("后台任务 %s 意外退出: %s", name, exc)

    task.add_done_callback(_done)
    return task


async def cancel_and_wait(task: asyncio.Task | None) -> None:
    """取消并等待收尾，吞掉取消异常。"""
    if task is None:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
