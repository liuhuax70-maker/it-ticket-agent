"""Ollama 调用封装：本地生成模型，支持流式输出与耗时/token 计量。

调用要点（与重排同源的经验）：

- 必须传 `"think": false`，否则 Qwen3 系列模型的 `<think>` 会占满输出；
- 流式走 `/api/chat` + `stream=true`，响应是 NDJSON（每行一个 JSON）；
- 非流式调用做简单的退避重试，流式调用不做重试（避免重复输出）。

延迟相关（见 `编码过程/11-延迟优化.md`）：

- `num_predict`：限制输出长度。本场景回答常达数百 token，是延迟的主要来源；
- `keep_alive`：让模型在内存中保活，避免每次请求重新加载（实测加载耗时可达数秒~十几秒）；
- `generate_detailed()`：返回首 token 延迟（TTFT）与 Ollama 的 token 计量，用于剖析与成本观测。

超时与模型名来自配置（`OLLAMA_BASE_URL` / `LLM_MODEL` / `LLM_TIMEOUT_SECONDS`）。
"""

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass

import httpx

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger

logger = get_logger(__name__)

#: 非流式调用的重试次数（不含首次）
MAX_RETRIES = 2
#: 退避基数（秒），第 n 次重试等待 BACKOFF_BASE * 2**(n-1)
BACKOFF_BASE = 1.0

_NANOS = 1e9


@dataclass
class GenerationMetrics:
    """一次生成的耗时与 token 计量。"""

    model: str
    text: str
    total_seconds: float
    ttft_seconds: float | None
    load_seconds: float
    prompt_tokens: int
    output_tokens: int

    @property
    def output_tokens_per_second(self) -> float:
        """纯生成速率（tok/s），已剔除模型加载耗时。"""
        generating = self.total_seconds - self.load_seconds
        if generating <= 0 or not self.output_tokens:
            return 0.0
        return round(self.output_tokens / generating, 2)

    def to_dict(self) -> dict:
        data = self.__dict__.copy()
        data.pop("text", None)
        data["output_tokens_per_second"] = self.output_tokens_per_second
        return data


def _chat_endpoint() -> str:
    return f"{get_settings().ollama_base_url.rstrip('/')}/api/chat"


def _payload(
    system: str,
    user: str,
    *,
    model: str | None,
    stream: bool,
    num_predict: int | None = None,
    keep_alive: str | None = None,
) -> dict:
    """组装请求体。

    :param num_predict: 输出 token 上限；`None` 取配置，配置为 0/None 表示不限制。
    :param keep_alive: 模型保活时长（如 `30m`）；`None` 取配置。
    """
    settings = get_settings()

    options: dict = {"temperature": 0.2}
    predict = settings.llm_num_predict if num_predict is None else num_predict
    if predict and predict > 0:
        options["num_predict"] = predict

    payload: dict = {
        "model": model or settings.llm_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": stream,
        "think": False,  # 关键：不关掉 thinking 会拿到空/含思考过程的内容
        "options": options,
    }

    keep = settings.llm_keep_alive if keep_alive is None else keep_alive
    if keep:
        payload["keep_alive"] = keep
    return payload


def _log_metrics(model: str, data: dict) -> None:
    """把 Ollama 返回的耗时/token 计量打到日志（可观测与成本核算的基础）。"""
    total = (data.get("total_duration") or 0) / _NANOS
    load = (data.get("load_duration") or 0) / _NANOS
    eval_seconds = (data.get("eval_duration") or 0) / _NANOS
    output_tokens = int(data.get("eval_count") or 0)
    rate = output_tokens / eval_seconds if eval_seconds > 0 else 0.0

    logger.info(
        "生成计量: model=%s total=%.1fs load=%.1fs prompt_tokens=%d output_tokens=%d 速率=%.1f tok/s",
        model,
        total,
        load,
        int(data.get("prompt_eval_count") or 0),
        output_tokens,
        rate,
    )


def generate_stream(
    system: str,
    user: str,
    *,
    model: str | None = None,
    timeout: float | None = None,
    num_predict: int | None = None,
    keep_alive: str | None = None,
) -> Iterator[str]:
    """流式生成，逐段 yield 文本增量（对接 SSE 的 token 事件）。

    :raises AppError: 生成服务不可用（错误码 2002）。
    """
    settings = get_settings()
    request_timeout = timeout if timeout is not None else settings.llm_timeout_seconds
    payload = _payload(
        system, user, model=model, stream=True, num_predict=num_predict, keep_alive=keep_alive
    )

    try:
        with httpx.stream("POST", _chat_endpoint(), json=payload, timeout=request_timeout) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("无法解析流式响应行，已跳过: %s", line[:80])
                    continue

                delta = (data.get("message") or {}).get("content") or ""
                if delta:
                    yield delta
                if data.get("done"):
                    break
    except httpx.HTTPError as exc:
        raise AppError(
            ErrorCode.GENERATION_SERVICE_UNAVAILABLE,
            f"生成服务调用失败: {exc}",
        ) from exc


def generate_detailed(
    system: str,
    user: str,
    *,
    model: str | None = None,
    timeout: float | None = None,
    num_predict: int | None = None,
    keep_alive: str | None = None,
) -> GenerationMetrics:
    """流式生成并返回完整计量（TTFT / 总耗时 / token 数）。

    用于延迟剖析与成本观测：流式才能测出**首 token 延迟**，
    而 Ollama 的最终分片里带有 `eval_count` 等精确计量。

    :raises AppError: 生成服务不可用（错误码 2002）。
    """
    settings = get_settings()
    request_timeout = timeout if timeout is not None else settings.llm_timeout_seconds
    used_model = model or settings.llm_model
    payload = _payload(
        system, user, model=model, stream=True, num_predict=num_predict, keep_alive=keep_alive
    )

    start = time.perf_counter()
    ttft: float | None = None
    parts: list[str] = []
    final: dict = {}

    try:
        with httpx.stream("POST", _chat_endpoint(), json=payload, timeout=request_timeout) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                delta = (data.get("message") or {}).get("content") or ""
                if delta:
                    if ttft is None:
                        ttft = time.perf_counter() - start
                    parts.append(delta)
                if data.get("done"):
                    final = data
                    break
    except httpx.HTTPError as exc:
        raise AppError(
            ErrorCode.GENERATION_SERVICE_UNAVAILABLE,
            f"生成服务调用失败: {exc}",
        ) from exc

    metrics = GenerationMetrics(
        model=used_model,
        text="".join(parts),
        total_seconds=round(time.perf_counter() - start, 3),
        ttft_seconds=round(ttft, 3) if ttft is not None else None,
        load_seconds=round((final.get("load_duration") or 0) / _NANOS, 3),
        prompt_tokens=int(final.get("prompt_eval_count") or 0),
        output_tokens=int(final.get("eval_count") or 0),
    )
    _log_metrics(used_model, final)
    return metrics


def generate(
    system: str,
    user: str,
    *,
    model: str | None = None,
    timeout: float | None = None,
    num_predict: int | None = None,
    keep_alive: str | None = None,
) -> str:
    """非流式生成，返回完整文本（失败时退避重试）。

    :raises AppError: 重试耗尽仍失败（错误码 2002）。
    """
    settings = get_settings()
    request_timeout = timeout if timeout is not None else settings.llm_timeout_seconds
    used_model = model or settings.llm_model
    payload = _payload(
        system, user, model=model, stream=False, num_predict=num_predict, keep_alive=keep_alive
    )
    last_error: Exception | None = None

    for attempt in range(MAX_RETRIES + 1):
        try:
            response = httpx.post(_chat_endpoint(), json=payload, timeout=request_timeout)
            response.raise_for_status()
            data = response.json()
            _log_metrics(used_model, data)
            return (data.get("message") or {}).get("content", "")
        except httpx.HTTPError as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                delay = BACKOFF_BASE * (2**attempt)
                logger.warning(
                    "生成调用失败（第 %d/%d 次），%.1fs 后重试: %s",
                    attempt + 1,
                    MAX_RETRIES + 1,
                    delay,
                    exc,
                )
                time.sleep(delay)

    raise AppError(
        ErrorCode.GENERATION_SERVICE_UNAVAILABLE,
        f"生成服务调用失败（已重试 {MAX_RETRIES} 次）: {last_error}",
    ) from last_error
