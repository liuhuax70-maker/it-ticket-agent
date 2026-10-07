"""model-gateway 路由层单元测试（不触网）。"""

from __future__ import annotations

import pytest
from app.fallback import FallbackPolicy
from app.router import build_messages, format_context

from packages.contracts import ContextItem, GenerateRequest
from packages.llms import build_target
from packages.llms.config import LLMSettings


def _req() -> GenerateRequest:
    return GenerateRequest(
        query="入职体检费用怎么报销？",
        contexts=[
            ContextItem(
                index=2,
                chunk_id="d_1:5",
                doc_id="d_1",
                doc_title="员工手册",
                section_path="第三章 福利 > 3.2 入职体检",
                text="报销上限为五百元。",
            ),
            ContextItem(
                index=1,
                chunk_id="d_1:4",
                doc_id="d_1",
                doc_title="员工手册",
                section_path="第三章 福利 > 3.2 入职体检",
                text="转正后凭发票报销。",
            ),
        ],
    )


def test_format_context_orders_by_index() -> None:
    """引用编号必须与 context 顺序一致：先 [1] 再 [2]。"""
    text = format_context(_req())
    assert text.index("[1]") < text.index("[2]")
    assert "员工手册 > 第三章 福利 > 3.2 入职体检" in text


def test_build_messages_renders_prompt_without_leftover_placeholders() -> None:
    messages = build_messages(_req())
    assert len(messages) == 1
    content = messages[0]["content"]
    assert "{{" not in content
    assert "入职体检费用怎么报销？" in content
    # v2 默认走哨兵标记；用户可见的拒答文案由编排层统一归一化，不出现在提示词里
    assert "NO_ANSWER" in content


def test_provider_switch() -> None:
    """provider 切换：deepseek 走官方 API，local 走本地 OpenAI 兼容端点。"""
    deepseek = LLMSettings(llm_provider="deepseek", deepseek_api_key="sk-test")
    target = build_target(deepseek)
    assert target.litellm_model == "deepseek/deepseek-chat"
    assert target.api_base is None
    assert target.extra == {}

    # 显式声明 api_style：不能依赖开发者本地 .env，否则测试结果随机器而变
    local = LLMSettings(
        llm_provider="local",
        local_llm_model="deepseek-r1:7b",
        local_llm_api_style="openai",
        local_llm_base_url="http://localhost:11434/v1",
    )
    target = build_target(local)
    assert target.litellm_model == "openai/deepseek-r1:7b"
    assert target.api_base == "http://localhost:11434/v1"
    assert target.extra == {}


def test_local_ollama_style_disables_thinking() -> None:
    """Ollama 原生风格：模型名加 ollama/ 前缀、api_base 去掉 /v1、并透传 think。

    思考型模型（qwen3 等）在 OpenAI 兼容接口下无法关闭思考链，输出预算会被
    思维链吃光导致 content 为空——这正是必须支持原生风格的原因。
    """
    settings = LLMSettings(
        llm_provider="local",
        local_llm_model="qwen3.5:4b",
        local_llm_api_style="ollama",
        local_llm_think=False,
        local_llm_base_url="http://localhost:11434/v1",
    )
    target = build_target(settings)
    assert target.litellm_model == "ollama/qwen3.5:4b"
    assert target.api_base == "http://localhost:11434"
    assert target.extra == {"think": False}


def test_explicit_litellm_name_bypasses_provider_resolution() -> None:
    """显式给完整 LiteLLM 名时直接透传，不再叠加 provider 解析。"""
    settings = LLMSettings(llm_provider="local", deepseek_api_key="sk-test")
    target = build_target(settings, "deepseek/deepseek-chat")
    assert target.provider == "deepseek"
    assert target.litellm_model == "deepseek/deepseek-chat"


def test_missing_key_raises() -> None:
    from packages.common.errors import ConfigError

    with pytest.raises(ConfigError):
        build_target(LLMSettings(llm_provider="deepseek", deepseek_api_key=""))


def test_fallback_policy() -> None:
    from packages.common.errors import ConfigError, UpstreamError

    policy = FallbackPolicy.from_settings(fallback_count=1, cooldown_seconds=0.1)
    assert policy.max_attempts == 2
    assert policy.is_retryable(UpstreamError("model-gateway", "boom"))
    assert not policy.is_retryable(ConfigError("bad config"))


# ---------------- 提示模板版本 ----------------


def test_prompt_versions_are_both_available() -> None:
    from packages.prompts import get_prompt_registry

    registry = get_prompt_registry()
    expected = {
        "v1": {"refuse_text", "context", "query"},
        "v2": {"refuse_marker", "context", "query"},
    }
    for version, required in expected.items():
        template = registry.get("rag_answer", version)
        placeholders = set(__import__("re").findall(r"\{\{(\w+)\}\}", template))
        assert required <= placeholders, f"{version} 缺少占位符: {required - placeholders}"


def test_v2_puts_requirements_after_question() -> None:
    """v2 把「回答要求」放在问题之后：指令离生成位置更近，模型更不容易忽略。"""
    from packages.prompts import get_prompt_registry

    template = get_prompt_registry().get("rag_answer", "v2")
    assert template.index("【参考资料】") < template.index("【问题】") < template.index("【回答要求】")
    # 必须显式给出「有资料就要答」的正向约束，否则小模型倾向一律拒答
    assert "必须" in template
    assert "只有当" in template
    # 拒答用机器可识别的哨兵，而不是让模型复述一段中文文案
    assert "{{refuse_marker}}" in template


def test_build_messages_accepts_prompt_version() -> None:
    content_v2 = build_messages(_req(), "v2")[0]["content"]
    content_v1 = build_messages(_req(), "v1")[0]["content"]
    assert content_v2 != content_v1
    assert "回答要求" in content_v2
