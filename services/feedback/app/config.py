"""feedback 配置。"""

from __future__ import annotations

from packages.common.constants import SERVICE_FEEDBACK
from packages.common.settings import BaseAppSettings


class Settings(BaseAppSettings):
    service_name: str = SERVICE_FEEDBACK
    host: str = "0.0.0.0"
    port: int = 8007

    database_url: str = "postgresql+asyncpg://rag:rag@localhost:5432/rag"
    # 坏例导出目录（喂给 eval 数据集）
    bad_case_dir: str = "./eval_data/bad_cases"

    # Kafka 通道（占位：false 时直接写库）
    use_kafka: bool = False
    kafka_bootstrap: str = "localhost:9092"
    kafka_topic_feedback: str = "feedback-events"
    kafka_consumer_group: str = "feedback"
