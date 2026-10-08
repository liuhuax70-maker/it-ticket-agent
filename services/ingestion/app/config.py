"""ingestion 配置。"""

from __future__ import annotations

from packages.common.constants import SERVICE_INGESTION
from packages.common.settings import BaseAppSettings


class Settings(BaseAppSettings):
    """ingestion 配置：元数据库、语料/切分参数、生命周期声明路径、下游 indexing 地址、

    ACL 默认值与 Kafka 通道开关（``use_kafka=False`` 时走同步直连，入库失败默认 fail-fast
    以保证 PG 与索引一致）。
    """

    service_name: str = SERVICE_INGESTION
    host: str = "0.0.0.0"
    port: int = 8004

    # ---- 元数据 ----
    database_url: str = "postgresql+asyncpg://rag:rag@localhost:5432/rag"

    # ---- 语料与切分 ----
    docs_dir: str = "./data/corpus"
    upload_dir: str = "./data/uploads"
    chunk_size: int = 500
    chunk_overlap: int = 80
    min_chunk_size: int = 40
    max_file_size_mb: int = 32

    # ---- 生命周期（失效管理）----
    # 声明式语料生命周期：哪些文档已废止、各自的生效/失效日期。
    # 文件不存在时全部按现行有效处理——不加声明不应改变任何既有行为。
    lifecycle_config_path: str = "configs/corpus/lifecycle.yaml"

    # ---- 下游 indexing ----
    indexing_url: str = "http://localhost:8005"
    indexing_timeout: float = 300.0
    # 入库失败是否让整个 ingest 失败（同步直连下应为 True，否则 PG 与索引不一致）
    fail_fast_on_index_error: bool = True

    # ---- ACL 默认值（P2 起由数据源抽取真实值）----
    default_tenant_id: str = "default"
    default_department_id: str = "default"

    # ---- Kafka 通道（占位：false 时走同步直连）----
    use_kafka: bool = False
    kafka_bootstrap: str = "localhost:9092"
    kafka_topic_raw_documents: str = "raw-documents"
    kafka_topic_chunk_events: str = "chunk-events"
    kafka_consumer_group: str = "ingestion"
