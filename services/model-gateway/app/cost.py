"""LLM 成本折算：token -> 美元。

为什么要折算：token 数不等于钱（不同模型差一个数量级），而"成本"要能回答的是
「这个功能每天烧多少美元」「换模型能省多少」。单价表放在配置里而不是代码里——
供应商调价是常态，改配置比重发版轻。

单价默认值是 2026-10 的 DeepSeek 官方价（缓存未命中口径），**会漂移**：
以供应商账单为准校准，别把这里的数字当成财务事实。
"""

from __future__ import annotations

import json
from typing import Any

from packages.common.logging import get_logger

logger = get_logger("model_gateway.cost")

# 默认单价（USD / 1M tokens）。前缀匹配：响应里的模型名常带 provider 前缀
# （如 deepseek/deepseek-chat），按最长前缀匹配到条目。
DEFAULT_PRICES: dict[str, dict[str, float]] = {
    "deepseek": {"input": 0.27, "output": 1.10},
    "local": {"input": 0.0, "output": 0.0},
    "ollama": {"input": 0.0, "output": 0.0},
}


def parse_prices(raw: str) -> dict[str, dict[str, float]]:
    """解析 LLM_PRICES 配置（JSON）。坏配置要响亮失败——静默用错单价比没有更糟。"""
    if not raw.strip():
        return dict(DEFAULT_PRICES)
    parsed = json.loads(raw)
    if not isinstance(parsed, dict) or not parsed:
        raise ValueError("LLM_PRICES 必须是非空 JSON 对象")
    for key, value in parsed.items():
        if not isinstance(value, dict) or "input" not in value or "output" not in value:
            raise ValueError(f"LLM_PRICES[{key!r}] 缺少 input/output 字段")
        if value["input"] < 0 or value["output"] < 0:
            raise ValueError(f"LLM_PRICES[{key!r}] 单价不能为负")
    return {**DEFAULT_PRICES, **parsed}


def match_price(model: str, prices: dict[str, dict[str, float]]) -> dict[str, float] | None:
    """按最长前缀匹配模型单价；未配置的模型返回 None（调用方决定记 0 还是告警）。"""
    model = model.lower()
    best: str | None = None
    for prefix in prices:
        if model.startswith(prefix.lower()) and (best is None or len(prefix) > len(best)):
            best = prefix
    return prices[best] if best else None


def cost_usd(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    prices: dict[str, dict[str, float]],
) -> float:
    """单次调用成本；未知模型返回 0.0 并告警一次性说明（避免每请求刷日志）。"""
    price = match_price(model, prices)
    if price is None:
        logger.warning("模型 %r 未配置单价，成本按 0 记（请在 LLM_PRICES 补充）", model)
        return 0.0
    cost = prompt_tokens / 1_000_000 * price["input"]
    cost += completion_tokens / 1_000_000 * price["output"]
    return round(cost, 6)


def usage_cost_usd(model: str, usage: dict[str, Any], prices: dict[str, dict[str, float]]) -> float:
    """从 LiteLLM 风格的 usage dict 算成本；字段缺失按 0 处理。"""
    return cost_usd(
        model,
        int(usage.get("prompt_tokens") or 0),
        int(usage.get("completion_tokens") or 0),
        prices,
    )
