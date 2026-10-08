"""Kafka 消费入口（占位）。

最小闭环走 ingestion -> indexing 的**同步 HTTP 直连**（USE_KAFKA=false）。
本模块把异步通道的接口固定下来：切异步时打开 USE_KAFKA 即可，业务编排
``IndexService.index`` 不需要改。
"""

from __future__ import annotations

from app.config import Settings
from app.service import IndexService
from packages.common.logging import get_logger
from packages.contracts import Chunk, IndexRequest

logger = get_logger("indexing.consumers")


async def consume_chunk_events(service: IndexService, settings: Settings) -> None:
    """消费 ``chunk-events`` 并入库。USE_KAFKA=false 时直接返回。"""
    if not settings.use_kafka:
        logger.info("USE_KAFKA=false，indexing 以同步直连模式工作，Kafka 消费者未启动")
        return

    from packages.common.kafka import consume

    logger.info(
        "启动 Kafka 消费者 topic=%s group=%s",
        settings.kafka_topic_chunk_events,
        settings.kafka_consumer_group,
    )
    async for payload in consume(
        settings.kafka_bootstrap, settings.kafka_topic_chunk_events, settings.kafka_consumer_group
    ):
        try:
            # 事件分两种形态：入库（带 chunks）与删除（op=delete）。
            # 此前只认入库形态：删除事件会构造出空 chunks 的 IndexRequest，
            # 被 service 拒绝后仅记一条 error——**合规删除静默失效**，
            # 文档已从台账移除但向量与正文永久残留、仍可被检索引用。
            if payload.get("op") == "delete":
                doc_id = str(payload.get("doc_id") or "")
                if not doc_id:
                    logger.error("删除事件缺少 doc_id，跳过: %s", payload)
                    continue
                deleted = await service.delete_document(doc_id)
                logger.info("Kafka 消费删除成功 doc_id=%s %s", doc_id, deleted)
                continue
            req = IndexRequest(
                chunks=[Chunk.model_validate(c) for c in payload.get("chunks", [])],
                reindex=bool(payload.get("reindex", False)),
            )
            result = await service.index(req)
            logger.info(
                "Kafka 消费入库成功 doc_id=%s chunks=%s", result.doc_id, result.chunks_indexed
            )
        except Exception as exc:  # noqa: BLE001 - 单条失败不应终止消费循环
            logger.error("Kafka 消费入库失败: %s", exc)
