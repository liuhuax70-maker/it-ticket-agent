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
    assert "抱歉" in content  # refuse_text 已被注入


def test_provider_switch() -> None:
    """provider 切换：deepseek 需要密钥，local 指向本地端点。"""
    deepseek = LLMSettings(llm_provider="deepseek", deepseek_api_key="sk-test")
    target = build_target(deepseek)
    assert target.litellm_model == "deepseek/deepseek-chat"
    assert target.api_base is None

    local = LLMSettings(llm_provider="local", local_llm_model="deepseek-r1:7b")
    target = build_target(local)
    assert target.litellm_model == "openai/deepseek-r1:7b"
    assert target.api_base == "http://localhost:11434/v1"


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
