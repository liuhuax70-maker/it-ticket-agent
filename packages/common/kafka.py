"""Kafka 接入占位。

最小闭环采用服务间**同步直连**（``USE_KAFKA=false``），本模块只固化拓扑与接口，
使后续切异步时不需要改动业务代码：

    raw-documents  : ingestion 产出（原始文档事件）
    chunk-events   : ingestion -> indexing（分块事件）

切换方式：把 ``USE_KAFKA`` 置为 true 并启动 ``--profile streaming``。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from packages.common.logging import get_logger

logger = get_logger("common.kafka")

try:  # aiokafka 为可选依赖，未安装时不阻塞服务启动
    from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

    KAFKA_AVAILABLE = True
except Exception:  # noqa: BLE001
    AIOKafkaConsumer = None  # type: ignore[assignment]
    AIOKafkaProducer = None  # type: ignore[assignment]
    KAFKA_AVAILABLE = False


class KafkaPublisher:
    """分块事件生产者（占位实现，接口已定型）。"""

    def __init__(self, bootstrap: str, topic: str) -> None:
        self.bootstrap = bootstrap
        self.topic = topic
        self._producer: Any = None

    async def start(self) -> None:
        if not KAFKA_AVAILABLE:
            raise RuntimeError("aiokafka 未安装，无法启用 Kafka 通道")
        self._producer = AIOKafkaProducer(
            bootstrap_servers=self.bootstrap,
            value_serializer=lambda v: json.dumps(v, ensure_ascii=False).encode("utf-8"),
        )
        await self._producer.start()

    async def stop(self) -> None:
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None

    async def publish(self, payload: dict[str, Any], key: str | None = None) -> None:
        if self._producer is None:
            raise RuntimeError("KafkaPublisher 未启动")
        await self._producer.send_and_wait(
            self.topic, value=payload, key=key.encode("utf-8") if key else None
        )


async def consume(bootstrap: str, topic: str, group_id: str) -> AsyncIterator[dict[str, Any]]:
    """消费循环（占位实现）。"""
    if not KAFKA_AVAILABLE:
        raise RuntimeError("aiokafka 未安装，无法启用 Kafka 通道")
    consumer = AIOKafkaConsumer(
        topic,
        bootstrap_servers=bootstrap,
        group_id=group_id,
        auto_offset_reset="earliest",
        enable_auto_commit=True,
    )
    await consumer.start()
    try:
        async for msg in consumer:
            try:
                yield json.loads(msg.value.decode("utf-8"))
            except Exception:  # noqa: BLE001
                logger.warning("丢弃无法反序列化的消息 offset=%s", msg.offset)
    finally:
        await consumer.stop()
