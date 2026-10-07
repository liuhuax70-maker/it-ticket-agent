"""model-gateway 配置。"""

from __future__ import annotations

from packages.common.constants import SERVICE_MODEL_GATEWAY
from packages.embeddings.config import EmbedSettings
from packages.llms.config import LLMSettings


class Settings(LLMSettings, EmbedSettings):
    service_name: str = SERVICE_MODEL_GATEWAY
    host: str = "0.0.0.0"
    port: int = 8003

    # LiteLLM Router 配置文件（模型清单与兜底链）
    litellm_config_path: str = "configs/models/litellm.yaml"

    # 兜底策略
    fallback_cooldown_seconds: float = 0.5

    # 租户配额（默认关闭，仅统计不影响调用）
    quota_enabled: bool = False
    quota_daily_tokens: int = 2_000_000
