"""成本折算（cost.py）的行为测试。

不连 Redis、不调模型：价格匹配与金额计算是纯函数，测它们就够。
配错单价是"悄悄多花钱"的一类问题，所以坏配置必须响亮失败。
"""

from __future__ import annotations

import json

import pytest
from app.cost import cost_usd, match_price, parse_prices, usage_cost_usd

PRICES = {
    **parse_prices(""),
    "deepseek/deepseek-reasoner": {"input": 0.55, "output": 2.19},
}


def test_parse_prices_default_when_empty() -> None:
    prices = parse_prices("")
    assert prices["deepseek"]["input"] > 0
    assert prices["local"]["input"] == 0.0, "本地模型成本必须是 0，不是缺省价"


def test_parse_prices_rejects_bad_config() -> None:
    with pytest.raises(ValueError):
        parse_prices("not json")
    with pytest.raises(ValueError):
        parse_prices(json.dumps({"deepseek": {"input": 0.1}}))
    with pytest.raises(ValueError):
        parse_prices(json.dumps({"deepseek": {"input": -1, "output": 1}}))


def test_match_price_prefers_longest_prefix() -> None:
    # deepseek/deepseek-reasoner 比 deepseek 更长，必须赢
    reasoner = match_price("deepseek/deepseek-reasoner", PRICES)
    assert reasoner == PRICES["deepseek/deepseek-reasoner"]
    assert match_price("deepseek/deepseek-chat", PRICES) == PRICES["deepseek"]
    ollama = match_price("ollama/qwen3-4b", PRICES)
    assert ollama is not None and ollama["input"] == 0.0
    assert match_price("unknown-model", PRICES) is None


def test_cost_usd_matches_hand_calculation() -> None:
    # 1M 输入 + 1M 输出 = 0.27 + 1.10 = 1.37
    assert cost_usd("deepseek/deepseek-chat", 1_000_000, 1_000_000, PRICES) == pytest.approx(1.37)
    # 1000 输入 + 500 输出
    assert cost_usd("deepseek/deepseek-chat", 1000, 500, PRICES) == pytest.approx(
        1000 / 1e6 * 0.27 + 500 / 1e6 * 1.10
    )


def test_cost_usd_is_zero_for_unknown_model() -> None:
    """未配置单价记 0 并告警——绝不能猜一个数。"""
    assert cost_usd("mystery/model", 1000, 1000, PRICES) == 0.0


def test_usage_cost_handles_missing_fields() -> None:
    assert usage_cost_usd("deepseek/deepseek-chat", {}, PRICES) == 0.0
    assert usage_cost_usd(
        "deepseek/deepseek-chat", {"prompt_tokens": 100, "completion_tokens": None}, PRICES
    ) == pytest.approx(100 / 1e6 * 0.27)
