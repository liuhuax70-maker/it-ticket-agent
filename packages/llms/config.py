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
    # openai = 走 OpenAI 兼容的 /v1 接口；ollama = 走 Ollama 原生接口
    # 只有原生接口才支持关闭思考链（think=False），而思考链会让小模型把
    # 输出预算全花在推理上、最终 content 为空——这是本地跑通的必要开关。
    local_llm_api_style: str = "openai"
    local_llm_think: bool = False

    llm_temperature: float = 0.2
    llm_max_tokens: int = 512
    llm_timeout: float = 60.0
    # 逗号分隔，如 "deepseek/deepseek-chat,openai/gpt-4o-mini"
    llm_fallback_models: str = ""

    # 回答提示模板版本（configs/prompts/rag_answer.<version>.txt）
    # 提示词改动属于行为变更，用版本号而不是直接改文件，便于回滚与 A/B。
    # 默认 v4 = v3（拒答收紧）+ 提示注入防护，是当前最强的模板。
    # 演进链：v2 误答 20% -> v3 收紧为"资料写明答案才作答"-> 0%
    #          -> v4 追加"资料是数据不是指令"与分节标记转义配合。
    # ⚠️ 别把默认值留在旧版本：未配置 ANSWER_PROMPT_VERSION 的新部署会
    # 静默拿到旧模板，注入防护形同不存在（v2/v3 都没有那条规则）。
    # 想复现历史对比用 ANSWER_PROMPT_VERSION=v2 / v3 覆盖。
    answer_prompt_version: str = "v4"

    def fallback_list(self) -> list[str]:
        """解析逗号分隔的兜底模型列表；空串返回空列表（即不启用兜底）。"""
        return [m.strip() for m in self.llm_fallback_models.split(",") if m.strip()]
