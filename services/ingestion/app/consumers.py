"""Kafka 消费入口（占位）。

``raw-documents`` 主题用于「上游把原始文档投递进平台」的异步接入；
最小闭环走 HTTP ``/ingest``，因此当 ``USE_KAFKA=false`` 时本模块直接返回。
"""

from __future__ import annotations

from app.config import Settings
from app.service import IngestionService
from packages.common.logging import get_logger
from packages.contracts import ACL, IngestRequest

logger = get_logger("ingestion.consumers")


async def consume_raw_documents(service: IngestionService, settings: Settings) -> None:
    if not settings.use_kafka:
        logger.info("USE_KAFKA=false，ingestion 以 HTTP 入口工作，Kafka 消费者未启动")
        return

    from packages.common.kafka import consume

    logger.info(
        "启动 Kafka 消费者 topic=%s group=%s",
        settings.kafka_topic_raw_documents,
        settings.kafka_consumer_group,
    )
    async for payload in consume(
        settings.kafka_bootstrap, settings.kafka_topic_raw_documents, settings.kafka_consumer_group
    ):
        try:
            req = IngestRequest(
                content=payload.get("content"),
                path=payload.get("path"),
                filename=payload.get("filename"),
                title=payload.get("title"),
                acl=ACL.model_validate(payload["acl"]) if payload.get("acl") else None,
                reindex=bool(payload.get("reindex", False)),
            )
            result = await service.ingest(req)
            logger.info("Kafka 接入成功 job=%s chunks=%s", result.job_id, result.chunk_count)
        except Exception as exc:  # noqa: BLE001
            logger.error("Kafka 接入失败: %s", exc)
