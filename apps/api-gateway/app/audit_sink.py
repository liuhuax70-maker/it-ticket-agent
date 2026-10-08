"""审计记录落库：内存队列 + 后台批量写入。

为什么不在中间件里直接 await 写库：
  1. 审计写失败绝不能拖垮业务请求——队列把两者解耦；
  2. 批量插入摊薄单条开销（审计是每请求一条的高频写入）。

丢弃策略（想清楚再改）：队列满或写库连续失败时**丢弃并告警日志**，
而不是阻塞或无限重试。理由：审计是"尽力持久"的旁路，stdout 里始终有一份；
为审计把 /chat 打挂是本末倒置。丢的每一段都有 warning 留痕，可据此配置告警。
"""

from __future__ import annotations

import asyncio
import time

from packages.common.db import create_all, session_scope
from packages.common.logging import get_logger
from packages.common.models import AuditLog

logger = get_logger("gateway.audit_sink")

_QUEUE_MAX = 5000
_BATCH = 100
_FLUSH_INTERVAL = 1.0


class AuditSink:
    def __init__(self, database_url: str) -> None:
        self._url = database_url
        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=_QUEUE_MAX)
        self._task: asyncio.Task | None = None
        self.dropped = 0

    async def start(self) -> None:
        # 开发/测试兜底建表；正式环境用 Alembic（见 packages/common/db.py）
        await create_all(self._url)
        self._task = asyncio.create_task(self._run(), name="audit-sink")

    async def put(self, record: dict) -> None:
        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            self.dropped += 1
            # 丢弃要留痕：审计断档必须可被发现，而不是悄悄少一段
            if self.dropped % 100 == 1:
                logger.warning("审计队列已满，累计丢弃 %s 条", self.dropped)

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            batch: list[dict] = []
            try:
                # 阻塞等第一条（可被取消）；拿到第一条后进入"攒批窗口"模式
                batch.append(await self._queue.get())
                deadline = loop.time() + _FLUSH_INTERVAL
                while len(batch) < _BATCH:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        break
                    try:
                        batch.append(await asyncio.wait_for(self._queue.get(), timeout=remaining))
                    except TimeoutError:
                        break
            except asyncio.CancelledError:
                # 退出前尽力把手里这批写完
                await self._write(batch)
                raise
            await self._write(batch)

    async def _write(self, batch: list[dict]) -> None:
        if not batch:
            return
        started = time.perf_counter()
        try:
            async with session_scope(self._url) as session:
                for record in batch:
                    session.add(AuditLog(**record))
            logger.debug(
                "审计落库 %s 条，耗时 %.0fms", len(batch), (time.perf_counter() - started) * 1000
            )
        except Exception as exc:  # noqa: BLE001
            self.dropped += len(batch)
            logger.warning("审计批量写入失败，丢弃 %s 条: %s", len(batch), exc)

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
