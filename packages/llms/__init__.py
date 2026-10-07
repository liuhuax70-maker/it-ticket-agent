"""LiteLLM 客户端与 DeepSeek 双模（官方 API / 本地 OpenAI 兼容端点）解析。"""

from packages.llms.client import LLMClient, ModelTarget, build_target
from packages.llms.config import LLMSettings

__all__ = ["LLMClient", "LLMSettings", "ModelTarget", "build_target"]
