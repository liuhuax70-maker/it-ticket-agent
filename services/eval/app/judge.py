"""RAGAS 裁判适配器：把项目自己的 LLMClient 接到 RAGAS 上。

为什么不直接用 ragas 自带的两种方式：

1. ``llm_factory(...)`` 走 OpenAI 兼容协议。本地裁判是思考型模型（qwen3 等），
   在兼容协议下**关不掉思考链**，输出预算被思维链吃光、content 为空，
   RAGAS 会拿到空字符串——所有指标 NaN，而且看不出原因。
2. ``langchain-ollama`` 能关思考链，但要多引一套依赖，而且模型解析逻辑
   会和项目里的 DeepSeek/本地双模解析分裂成两份。

复用自己的 LLMClient 之后：
    本地端点用 ``LOCAL_LLM_API_STYLE=ollama`` + ``LOCAL_LLM_THINK=false`` 就已经关掉思考链，
    切 DeepSeek 官方 API 时同一份代码也不用改。

另一个刻意的设计：``is_finished`` 判空返回 False。RAGAS 会因此抛
``LLMDidNotFinishException``——宁可响亮地失败，也不要静默产出一堆 NaN 分数。
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

from langchain_core.outputs import Generation, LLMResult
from ragas.llms.base import BaseRagasLLM

from packages.common.logging import get_logger
from packages.llms import LLMClient, build_target
from packages.llms.config import LLMSettings

logger = get_logger("eval.judge")


def _run_sync(coro: Any) -> Any:
    """在同步上下文里跑协程。

    RAGAS 既有同步路径也有异步路径；评测本身跑在 ``asyncio.to_thread`` 的工作线程里，
    那里没有事件循环，直接 ``asyncio.run`` 即可；万一上层塞了一个正在运行的事件循环，
    就退回到独立线程执行，避免 "This event loop is already running"。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    box: dict[str, Any] = {}

    def runner() -> None:
        box["value"] = asyncio.run(coro)

    thread = threading.Thread(target=runner)
    thread.start()
    thread.join()
    return box.get("value")


class ProjectLLMJudge(BaseRagasLLM):
    """用项目 LLMClient 实现的 RAGAS 裁判。"""

    def __init__(
        self,
        settings: LLMSettings,
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        model: str | None = None,
    ) -> None:
        super().__init__()
        self._settings = settings
        self._client = LLMClient(settings)
        self._target = build_target(settings, model)
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._prompt_tokens = 0
        self._completion_tokens = 0
        # 端点**实际服务**的模型名（取自响应），用于归因：
        # 中转/代理/平台侧常把请求的模型名映射成别的名字（例如 deepseek-chat -> deepseek-flash），
        # 只记请求名会让"模型变了"与"系统变差"分不开。
        self._served: str = ""

    @property
    def target_name(self) -> str:
        """请求的模型名（litellm 形态）。"""
        return self._target.litellm_model

    @property
    def served_name(self) -> str:
        """端点实际服务的模型名；尚未调用时为空串。"""
        return self._served

    @property
    def usage(self) -> dict[str, int]:
        return {"prompt": self._prompt_tokens, "completion": self._completion_tokens}

    @staticmethod
    def _prompt_text(prompt: Any) -> str:
        if hasattr(prompt, "to_string"):
            return str(prompt.to_string())
        if hasattr(prompt, "text"):
            return str(prompt.text)
        return str(prompt)

    async def agenerate_text(
        self,
        prompt: Any,
        n: int = 1,
        temperature: float | None = None,
        stop: list[str] | None = None,
        callbacks: Any = None,
    ) -> LLMResult:
        text = self._prompt_text(prompt)
        generations: list[Generation] = []
        for _ in range(max(1, n)):
            result = await self._client.complete_target(
                self._target,
                [{"role": "user", "content": text}],
                temperature=self._temperature if temperature is None else temperature,
                max_tokens=self._max_tokens,
            )
            usage = result.usage or {}
            self._prompt_tokens += int(usage.get("prompt_tokens", 0) or 0)
            self._completion_tokens += int(usage.get("completion_tokens", 0) or 0)
            if result.model and result.model != self._target.litellm_model:
                self._served = result.model
            generations.append(Generation(text=result.text or ""))
        return LLMResult(generations=[generations], llm_output={})

    def generate_text(
        self,
        prompt: Any,
        n: int = 1,
        temperature: float | None = 0.0,
        stop: list[str] | None = None,
        callbacks: Any = None,
    ) -> LLMResult:
        return _run_sync(
            self.agenerate_text(
                prompt, n=n, temperature=temperature, stop=stop, callbacks=callbacks
            )
        )

    def is_finished(self, response: LLMResult) -> bool:
        """空响应视为"没答完"。

        思考型模型把预算花在思维链上时会返回空串，这里返回 False 让 RAGAS 抛出
        LLMDidNotFinishException——比静默产出 NaN 分数好定位得多。
        """
        try:
            text = response.generations[0][0].text
        except (AttributeError, IndexError, TypeError):
            return False
        return bool(text and text.strip())


__all__ = ["ProjectLLMJudge", "_run_sync"]
