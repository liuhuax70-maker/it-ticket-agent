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

    # ---- 成本折算 ----
    # 单价表（USD / 1M tokens），JSON；键按**最长前缀**匹配响应里的模型名。
    # 默认价是 2026-10 的 DeepSeek 官方价，会漂移——以供应商账单为准校准。
    llm_prices_json: str = ""
