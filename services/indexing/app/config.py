"""indexing 配置：向量化 + 向量库 + 全文索引。"""

from __future__ import annotations

from packages.common.constants import SERVICE_INDEXING
from packages.embeddings.config import EmbedSettings
from packages.search.config import OpenSearchSettings
from packages.vectorstores.config import MilvusSettings


class Settings(EmbedSettings, MilvusSettings, OpenSearchSettings):
    service_name: str = SERVICE_INDEXING
    host: str = "0.0.0.0"
    port: int = 8005

    # Kafka 通道（最小闭环 USE_KAFKA=false，走 ingestion 同步直连）
    use_kafka: bool = False
    kafka_bootstrap: str = "localhost:9092"
    kafka_topic_chunk_events: str = "chunk-events"
    kafka_consumer_group: str = "indexing"
