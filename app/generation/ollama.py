"""Ollama 调用封装：本地生成模型，支持流式输出。

调用要点（与重排同源的经验）：
- 必须传 `"think": false`，否则 Qwen3 系列模型的 `<think>` 会占满输出；
- 流式走 `/api/chat` + `stream=true`，响应是 NDJSON（每行一个 JSON）；
- 非流式调用做简单的退避重试，流式调用不做重试（避免重复输出）。

超时与模型名来自配置（`OLLAMA_BASE_URL` / `LLM_MODEL` / `LLM_TIMEOUT_SECONDS`）。
"""

import json
import time
from collections.abc import Iterator

import httpx

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger

logger = get_logger(__name__)

#: 非流式调用的重试次数（不含首次）
MAX_RETRIES = 2
#: 退避基数（秒），第 n 次重试等待 BACKOFF_BASE * 2**(n-1)
BACKOFF_BASE = 1.0


def _chat_endpoint() -> str:
    return f"{get_settings().ollama_base_url.rstrip('/')}/api/chat"


def _payload(system: str, user: str, *, model: str | None, stream: bool) -> dict:
    settings = get_settings()
    return {
        "model": model or settings.llm_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": stream,
        "think": False,  # 关键：不关掉 thinking 会拿到空/含思考过程的内容
        "options": {"temperature": 0.2},
    }


def generate_stream(
    system: str,
    user: str,
    *,
    model: str | None = None,
    timeout: float | None = None,
) -> Iterator[str]:
    """流式生成，逐段 yield 文本增量（对接 SSE 的 token 事件）。

    :raises AppError: 生成服务不可用（错误码 2002）。
    """
    settings = get_settings()
    request_timeout = timeout if timeout is not None else settings.llm_timeout_seconds

    try:
        with httpx.stream(
            "POST",
            _chat_endpoint(),
            json=_payload(system, user, model=model, stream=True),
            timeout=request_timeout,
        ) as response:
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


def generate(
    system: str,
    user: str,
    *,
    model: str | None = None,
    timeout: float | None = None,
) -> str:
    """非流式生成，返回完整文本（失败时退避重试）。

    :raises AppError: 重试耗尽仍失败（错误码 2002）。
    """
    settings = get_settings()
    request_timeout = timeout if timeout is not None else settings.llm_timeout_seconds
    payload = _payload(system, user, model=model, stream=False)
    last_error: Exception | None = None

    for attempt in range(MAX_RETRIES + 1):
        try:
            response = httpx.post(_chat_endpoint(), json=payload, timeout=request_timeout)
            response.raise_for_status()
            return (response.json().get("message") or {}).get("content", "")
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
