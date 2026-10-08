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
    AIOKafkaConsumer = None
    AIOKafkaProducer = None
    KAFKA_AVAILABLE = False


class KafkaPublisher:
    """分块事件生产者（占位实现，接口已定型）。"""

    def __init__(self, bootstrap: str, topic: str) -> None:
        self.bootstrap = bootstrap
        self.topic = topic
        self._producer: Any = None

    async def start(self) -> None:
        """启动生产者；未安装 aiokafka 时显式报错（而不是静默不工作）。"""
        if not KAFKA_AVAILABLE:
            raise RuntimeError("aiokafka 未安装，无法启用 Kafka 通道")
        self._producer = AIOKafkaProducer(
            bootstrap_servers=self.bootstrap,
            value_serializer=lambda v: json.dumps(v, ensure_ascii=False).encode("utf-8"),
        )
        await self._producer.start()

    async def stop(self) -> None:
        """停止生产者并释放底层连接。"""
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None

    async def publish(self, payload: dict[str, Any], key: str | None = None) -> None:
        """发布一条分块事件；``key`` 用于分区（建议用 doc_id 保证同文档顺序）。"""
        if self._producer is None:
            raise RuntimeError("KafkaPublisher 未启动")
        await self._producer.send_and_wait(
            self.topic, value=payload, key=key.encode("utf-8") if key else None
        )


async def consume(bootstrap: str, topic: str, group_id: str) -> AsyncIterator[dict[str, Any]]:
    """消费循环（占位实现）。

    ⚠️ 语义是"至少投递一次 + 尽力而为"，不是"精确一次"：
        * ``enable_auto_commit=True``：位点在 **yield 之前**就已提交，
          所以处理失败也不会重投。不丢消息靠的是下游按 chunk_id 幂等 upsert。
        * 反序列化失败的消息直接丢弃，只打一条 warning，**没有计数指标也没有 DLQ**。
          触发原因通常是 schema 不匹配（生产者升级了 payload 而消费者没跟上）。
          要做到不丢，需引入死信 topic，或至少暴露一个丢弃计数指标。
        * 各服务 consumers.py 里捕获 handler 异常后继续循环，同样没有重试与 DLQ。
    """
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
                # 丢消息点：offset 已提交、不会重投，schema 不匹配时会静默丢数据
    finally:
        await consumer.stop()
