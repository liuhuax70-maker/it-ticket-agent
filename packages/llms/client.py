"""LiteLLM 封装：模型解析、调用、兜底降级。

设计要点：
    * provider 解析集中在一处（``build_target``），业务代码不感知 provider 差异；
    * 兜底链（fallback）与主调用共用同一实现，避免两套逻辑分叉；
    * 未配置密钥时抛 ``ConfigError``，错误信息直接告诉使用者该配哪个变量。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from packages.common.errors import ConfigError, UpstreamError
from packages.common.logging import get_logger
from packages.llms.config import LLMSettings

logger = get_logger("llms.client")


@dataclass(frozen=True)
class ModelTarget:
    """一次调用的目标模型（已解析为 LiteLLM 可识别的形态）。"""

    name: str
    provider: str
    litellm_model: str
    api_base: str | None = None
    api_key: str | None = None
    # provider 特有参数（如 Ollama 的 think=False），原样透传给 LiteLLM
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class CompletionResult:
    """一次模型调用的归一化结果：答案文本、实际模型/供应商、token 用量与耗时。

    不含任何密钥；``describe`` 返回的健康信息也遵循同样的「不泄露密钥」原则。
    """

    text: str
    model: str
    provider: str
    usage: dict[str, int] = field(default_factory=dict)
    timings_ms: dict[str, float] = field(default_factory=dict)


def _target_from_full_name(name: str, settings: LLMSettings) -> ModelTarget:
    """``provider/model`` 形式的完整 LiteLLM 名：按前缀推断连什么端点。"""
    prefix = name.split("/", 1)[0]
    is_deepseek = prefix == "deepseek"
    return ModelTarget(
        name=name,
        provider=prefix,
        litellm_model=name,
        api_base=(settings.deepseek_api_base or None)
        if is_deepseek
        else settings.local_llm_base_url,
        api_key=(
            settings.deepseek_api_key if is_deepseek else (settings.local_llm_api_key or None)
        ),
    )


def _deepseek_target(settings: LLMSettings) -> ModelTarget:
    """DeepSeek 官方 API。缺少密钥直接报错——静默降级到别的模型会掩盖配置错误。"""
    if not settings.deepseek_api_key:
        raise ConfigError(
            "缺少 DEEPSEEK_API_KEY：请在 .env 配置官方密钥，"
            "或设置 LLM_PROVIDER=local 走本地 OpenAI 兼容端点"
        )
    model = settings.deepseek_model
    return ModelTarget(
        name=model,
        provider="deepseek",
        litellm_model=f"deepseek/{model}",
        api_base=settings.deepseek_api_base or None,
        api_key=settings.deepseek_api_key,
    )


def _local_target(settings: LLMSettings) -> ModelTarget:
    """本地端点。按 ``LOCAL_LLM_API_STYLE`` 选协议。

    两种协议的差别很关键：只有 **Ollama 原生协议**能传 ``think=False`` 关掉思考链；
    OpenAI 兼容协议下思考型模型会把预算花在思维链上、content 为空。
    """
    model = settings.local_llm_model
    style = (settings.local_llm_api_style or "openai").lower()

    if style == "ollama" and "/" not in model:
        # Ollama 原生接口挂在根路径下，所以要去掉 base_url 里的 /v1 后缀
        base = settings.local_llm_base_url.rstrip("/")
        if base.endswith("/v1"):
            base = base[: -len("/v1")]
        return ModelTarget(
            name=model,
            provider="ollama",
            litellm_model=f"ollama/{model}",
            api_base=base,
            api_key=settings.local_llm_api_key or "ollama",
            extra={"think": settings.local_llm_think},
        )

    return ModelTarget(
        name=model,
        provider="local",
        litellm_model=model if "/" in model else f"openai/{model}",
        api_base=settings.local_llm_base_url,
        api_key=settings.local_llm_api_key or "not-needed",
    )


def build_target(settings: LLMSettings, model_override: str | None = None) -> ModelTarget:
    """把配置解析成 LiteLLM 调用目标（按 provider 分派）。

    ``model_override`` 支持两种写法：
        * 逻辑名 ``deepseek`` / ``local``：切到对应 provider 的默认模型
        * 完整 LiteLLM 名 ``deepseek/deepseek-chat``：直接透传

    ⚠️ 已知陷阱：``model_override`` 若是**既非上述逻辑名、也不含 "/"** 的值
    （例如 ``gpt-4o``、``qwen3.5:4b``），三个分支全部落空，函数会**静默**返回
    当前 provider 的默认模型——不抛异常、不打日志。后果是
    ``LLM_FALLBACK_MODELS=gpt-4o`` 这种配置看起来生效了，实际兜底成了"对同一模型
    重试一次"。新增来源的模型名时，要么用完整 LiteLLM 名，要么在这里补显式分支。
    """
    provider = (settings.llm_provider or "deepseek").lower()
    if model_override == "deepseek":
        provider = "deepseek"
    elif model_override == "local":
        provider = "local"
    elif model_override and "/" in model_override:
        return _target_from_full_name(model_override, settings)

    if provider == "deepseek":
        return _deepseek_target(settings)
    if provider == "local":
        return _local_target(settings)
    raise ConfigError(f"不支持的 LLM_PROVIDER={provider!r}（可选：deepseek | local）")


class LLMClient:
    """异步对话客户端。"""

    def __init__(self, settings: LLMSettings) -> None:
        self.settings = settings

    # ---------------- 内部 ----------------
    async def _call(
        self,
        target: ModelTarget,
        messages: list[dict[str, str]],
        *,
        temperature: float | None,
        max_tokens: int | None,
    ) -> CompletionResult:
        import litellm  # 延迟导入：litellm 导入较慢

        started = time.perf_counter()
        kwargs: dict[str, Any] = {
            "model": target.litellm_model,
            "messages": messages,
            "temperature": (self.settings.llm_temperature if temperature is None else temperature),
            "max_tokens": self.settings.llm_max_tokens if max_tokens is None else max_tokens,
            "timeout": self.settings.llm_timeout,
            "api_key": target.api_key,
        }
        if target.api_base:
            kwargs["api_base"] = target.api_base
        # provider 特有参数（如 ollama 的 think）
        kwargs.update(target.extra)

        try:
            resp = await litellm.acompletion(**kwargs)
        except Exception as exc:  # noqa: BLE001 - 统一转成 UpstreamError 供上层降级判断
            raise UpstreamError("model-gateway", f"{target.name} 调用失败: {exc}") from exc

        elapsed = (time.perf_counter() - started) * 1000
        content = (resp.choices[0].message.content or "").strip()
        usage_obj = getattr(resp, "usage", None)
        usage = {
            "prompt_tokens": int(getattr(usage_obj, "prompt_tokens", 0) or 0),
            "completion_tokens": int(getattr(usage_obj, "completion_tokens", 0) or 0),
            "total_tokens": int(getattr(usage_obj, "total_tokens", 0) or 0),
        }
        return CompletionResult(
            text=content,
            model=getattr(resp, "model", None) or target.name,
            provider=target.provider,
            usage=usage,
            timings_ms={"generate": round(elapsed, 1)},
        )

    async def complete_target(
        self,
        target: ModelTarget,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> CompletionResult:
        """按显式指定的目标模型调用一次，不做兜底（兜底策略由调用方决定）。"""
        return await self._call(target, messages, temperature=temperature, max_tokens=max_tokens)

    # ---------------- 对外 ----------------
    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        allow_fallback: bool = True,
    ) -> CompletionResult:
        """主调用入口：解析目标模型，按 fallback 链依次尝试，全部失败抛 ``UpstreamError``。

        ``model`` 缺省用配置默认；``allow_fallback=False`` 时只打主模型（兜底由调用方决定）。
        """
        primary = build_target(self.settings, model)
        errors: list[str] = []
        candidates: list[ModelTarget] = [primary]

        if allow_fallback:
            for name in self.settings.fallback_list():
                try:
                    candidates.append(build_target(self.settings, name))
                except ConfigError as exc:  # 兜底项配置不全只记录，不阻断主流程
                    logger.warning("跳过无效兜底模型 %s: %s", name, exc)

        for idx, target in enumerate(candidates):
            try:
                result = await self._call(
                    target, messages, temperature=temperature, max_tokens=max_tokens
                )
                if idx > 0:
                    logger.warning("主模型失败，已降级到 %s", target.name)
                return result
            except UpstreamError as exc:
                errors.append(str(exc))
                logger.error("模型调用失败 target=%s err=%s", target.name, exc)

        raise UpstreamError("model-gateway", "全部模型均失败: " + " | ".join(errors))

    def describe(self) -> dict[str, Any]:
        """不泄露密钥的自描述，用于 /health 与 /models。"""
        provider = (self.settings.llm_provider or "deepseek").lower()
        info: dict[str, Any] = {
            "provider": provider,
            "fallbacks": self.settings.fallback_list(),
            "prompt_version": self.settings.answer_prompt_version,
        }
        if provider == "deepseek":
            info["api_base"] = self.settings.deepseek_api_base or "https://api.deepseek.com"
            info["model"] = self.settings.deepseek_model
            info["key_configured"] = bool(self.settings.deepseek_api_key)
        else:
            info["api_base"] = self.settings.local_llm_base_url
            info["model"] = self.settings.local_llm_model
            info["api_style"] = self.settings.local_llm_api_style
            info["think"] = self.settings.local_llm_think
            info["key_configured"] = True
        return info
