"""Kafka 消费入口（占位）。

``feedback-events`` 用于网关把反馈异步投递过来（高并发场景避免直接压库）；
最小闭环走 HTTP 同步写入（USE_KAFKA=false）。
"""

from __future__ import annotations

from app.config import Settings
from app.store import FeedbackStore
from packages.common.logging import get_logger

logger = get_logger("feedback.consumers")


async def consume_feedback_events(store: FeedbackStore, settings: Settings) -> None:
    """消费 ``feedback-events`` 写库；``USE_KAFKA=false`` 时直接返回（走 HTTP 提交）。"""
    if not settings.use_kafka:
        logger.info("USE_KAFKA=false，feedback 以 HTTP 写入模式工作，Kafka 消费者未启动")
        return

    from packages.common.kafka import consume

    logger.info("启动 Kafka 消费者 topic=%s", settings.kafka_topic_feedback)
    async for payload in consume(
        settings.kafka_bootstrap, settings.kafka_topic_feedback, settings.kafka_consumer_group
    ):
        try:
            # 返回 (id, is_duplicate)；消费侧不关心重复，忽略即可
            await store.add(
                tenant_id=payload.get("tenant_id", "default"),
                user_id=payload.get("user_id", ""),
                query=payload.get("query", ""),
                answer=payload.get("answer", ""),
                rating=payload.get("rating"),
                comment=payload.get("comment"),
                trace_id=payload.get("trace_id"),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("反馈写入失败: %s", exc)
