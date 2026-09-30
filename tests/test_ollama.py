"""Ollama 封装的请求构造与计量单元测试（不依赖真实模型）。"""

from app.core.config import get_settings
from app.generation.ollama import GenerationMetrics, _payload


# ---------------- 请求构造 ----------------


def test_payload_disables_thinking():
    """必须关掉 thinking，否则 Qwen3 系列会输出 <think> 导致内容为空。"""
    payload = _payload("sys", "user", model="m", stream=False)
    assert payload["think"] is False


def test_payload_sets_low_temperature_for_stability():
    payload = _payload("sys", "user", model="m", stream=False)
    assert payload["options"]["temperature"] == 0.2


def test_payload_omits_num_predict_when_unlimited():
    """num_predict=0 表示不限制，不应出现在 options 里。"""
    payload = _payload("sys", "user", model="m", stream=False, num_predict=0)
    assert "num_predict" not in payload["options"]


def test_payload_sets_num_predict_when_positive():
    payload = _payload("sys", "user", model="m", stream=False, num_predict=256)
    assert payload["options"]["num_predict"] == 256


def test_payload_sets_keep_alive():
    """保活可避免本地模型每次请求重新加载（实测冷加载 12~13s）。"""
    payload = _payload("sys", "user", model="m", stream=False, keep_alive="30m")
    assert payload["keep_alive"] == "30m"


def test_payload_omits_keep_alive_when_blank():
    payload = _payload("sys", "user", model="m", stream=False, keep_alive="")
    assert "keep_alive" not in payload


def test_payload_falls_back_to_configured_model():
    payload = _payload("sys", "user", model=None, stream=False)
    assert payload["model"] == get_settings().llm_model


def test_payload_carries_system_and_user_messages():
    payload = _payload("S", "U", model="m", stream=False)
    assert [m["role"] for m in payload["messages"]] == ["system", "user"]
    assert payload["messages"][0]["content"] == "S"
    assert payload["messages"][1]["content"] == "U"


# ---------------- 计量 ----------------


def test_tokens_per_second_excludes_model_load():
    """生成速率应剔除加载耗时，否则冷启动会把速率算低。"""
    metrics = GenerationMetrics(
        model="m",
        text="x",
        total_seconds=10.0,
        ttft_seconds=2.0,
        load_seconds=4.0,
        prompt_tokens=100,
        output_tokens=60,
    )
    assert metrics.output_tokens_per_second == 10.0  # 60 / (10 - 4)


def test_tokens_per_second_is_zero_when_nothing_generated():
    metrics = GenerationMetrics(
        model="m",
        text="",
        total_seconds=3.0,
        ttft_seconds=None,
        load_seconds=3.0,
        prompt_tokens=0,
        output_tokens=0,
    )
    assert metrics.output_tokens_per_second == 0.0


def test_to_dict_excludes_generated_text():
    """计量字典会被写进报告，不应把回答正文带进去。"""
    metrics = GenerationMetrics(
        model="m",
        text="很长的回答正文",
        total_seconds=1.0,
        ttft_seconds=0.5,
        load_seconds=0.0,
        prompt_tokens=10,
        output_tokens=5,
    )

    data = metrics.to_dict()

    assert "text" not in data
    assert data["output_tokens"] == 5
    assert data["output_tokens_per_second"] == 5.0
