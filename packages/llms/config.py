"""LLM 相关配置。"""

from __future__ import annotations

from packages.common.settings import BaseAppSettings


class LLMSettings(BaseAppSettings):
    """LLM 网关配置。provider 决定走官方 API 还是本地端点。"""

    service_name: str = "model-gateway"

    # deepseek = 官方 API（https://api.deepseek.com）；local = 任意 OpenAI 兼容端点
    llm_provider: str = "deepseek"

    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-chat"
    # 官方端点可覆盖（如代理），留空用 LiteLLM 默认
    deepseek_api_base: str = ""

    local_llm_base_url: str = "http://localhost:11434/v1"
    local_llm_api_key: str = "ollama"
    local_llm_model: str = "deepseek-r1:7b"

    llm_temperature: float = 0.2
    llm_max_tokens: int = 1024
    llm_timeout: float = 60.0
    # 逗号分隔，如 "deepseek/deepseek-chat,openai/gpt-4o-mini"
    llm_fallback_models: str = ""

    def fallback_list(self) -> list[str]:
        return [m.strip() for m in self.llm_fallback_models.split(",") if m.strip()]
