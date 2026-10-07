"""模型路由：协议适配（内部契约 <-> 对话消息）、降级链、配额。"""

from __future__ import annotations

import asyncio
import time

from app.config import Settings
from app.fallback import FallbackPolicy
from app.quota import QuotaGuard
from packages.common.constants import REFUSE_MARKER, REFUSE_TEXT
from packages.common.errors import UpstreamError
from packages.common.logging import get_logger
from packages.contracts import (
    CompletionRequest,
    EmbedRequest,
    EmbedResponse,
    GenerateRequest,
    GenerateResponse,
)
from packages.embeddings import get_embedder
from packages.llms import LLMClient, ModelTarget, build_target
from packages.prompts import get_prompt_registry

logger = get_logger("model_gateway.router")

# 默认提示版本；实际取值由 Settings.answer_prompt_version 传入
DEFAULT_PROMPT_VERSION = "v3"


def format_context(req: GenerateRequest) -> str:
    """把 context 拼成带编号的文本块。

    编号顺序 = 引用编号顺序：编排器的 citations 映射必须与这里**共用同一列表**，
    否则会出现「引用张冠李戴」（旧 P0 坑位 #8）。
    """
    if not req.contexts:
        return "（无参考资料）"
    blocks: list[str] = []
    for item in sorted(req.contexts, key=lambda c: c.index):
        header = " > ".join(x for x in [item.doc_title, item.section_path] if x)
        prefix = f"[{item.index}] {header}" if header else f"[{item.index}]"
        blocks.append(f"{prefix}\n{item.text}")
    return "\n\n".join(blocks)


def build_messages(
    req: GenerateRequest, prompt_version: str = DEFAULT_PROMPT_VERSION
) -> list[dict[str, str]]:
    registry = get_prompt_registry()
    # 两个变量都传：v1 用 refuse_text，v2 用 refuse_marker，便于版本切换时互不影响
    user_content = registry.render(
        "rag_answer",
        prompt_version,
        refuse_text=REFUSE_TEXT,
        refuse_marker=REFUSE_MARKER,
        context=format_context(req),
        query=req.query,
    )
    messages: list[dict[str, str]] = []
    if req.system:
        messages.append({"role": "system", "content": req.system})
    messages.append({"role": "user", "content": user_content})
    return messages


class ModelRouter:
    """默认模型 + 兜底链的执行者。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = LLMClient(settings)
        self._quota = QuotaGuard(
            settings.redis_url, settings.quota_daily_tokens, settings.quota_enabled
        )

    # ---------------- 目标解析 ----------------
    def targets(self, model_override: str | None = None) -> list[ModelTarget]:
        """主模型 + 配置的兜底模型。配置不全的兜底项会被跳过而不是整体失败。"""
        chain: list[ModelTarget] = [build_target(self._settings, model_override)]
        if model_override:
            return chain  # 显式指定模型时不叠加兜底，避免调用方预期被破坏
        for name in self._settings.fallback_list():
            try:
                chain.append(build_target(self._settings, name))
            except Exception as exc:  # noqa: BLE001
                logger.warning("跳过无效兜底模型 %s: %s", name, exc)
        return chain

    def policy(self) -> FallbackPolicy:
        return FallbackPolicy.from_settings(
            len(self._settings.fallback_list()), self._settings.fallback_cooldown_seconds
        )

    # ---------------- 内部：带降级的执行 ----------------
    async def _run(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None,
        temperature: float | None,
        max_tokens: int | None,
        tenant_id: str | None,
    ) -> GenerateResponse:
        chain = self.targets(model)
        policy = self.policy()
        attempts = chain[: policy.max_attempts]
        errors: list[str] = []
        started_total = time.perf_counter()

        for idx, target in enumerate(attempts):
            try:
                result = await self._client.complete_target(
                    target, messages, temperature=temperature, max_tokens=max_tokens
                )
                if idx > 0:
                    logger.warning("主模型不可用，已降级到 %s", target.name)
                await self._quota.consume(tenant_id, result.usage.get("total_tokens", 0))
                timings = dict(result.timings_ms)
                timings["total"] = round((time.perf_counter() - started_total) * 1000, 1)
                return GenerateResponse(
                    answer=result.text,
                    model=result.model,
                    provider=result.provider,
                    usage=result.usage,
                    timings_ms=timings,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{target.name}: {exc}")
                logger.error("模型调用失败 target=%s err=%s", target.name, exc)
                if not policy.is_retryable(exc) or idx == len(attempts) - 1:
                    break
                await asyncio.sleep(policy.cooldown_seconds)

        raise UpstreamError("model-gateway", "全部模型均失败: " + " | ".join(errors))

    # ---------------- 业务 ----------------
    async def generate(self, req: GenerateRequest) -> GenerateResponse:
        """RAG 生成：上下文 + 引用约束提示词。"""
        return await self._run(
            build_messages(req, self._settings.answer_prompt_version),
            model=req.model,
            temperature=req.temperature,
            max_tokens=req.max_tokens,
            tenant_id=req.tenant_id,
        )

    async def complete(self, req: CompletionRequest) -> GenerateResponse:
        """原始补全：调用方自带 prompt（查询改写、合规审核等）。"""
        messages = [m.model_dump() for m in req.messages]
        return await self._run(
            messages,
            model=req.model,
            temperature=req.temperature,
            max_tokens=req.max_tokens,
            tenant_id=req.tenant_id,
        )

    async def embed(self, req: EmbedRequest) -> EmbedResponse:
        embedder = get_embedder(self._settings)
        vectors = await embedder.embed(req.texts, kind=req.kind)
        return EmbedResponse(vectors=vectors, dim=embedder.dim, model=embedder.model_name)

    async def quota_snapshot(self, tenant_id: str) -> dict[str, int | bool]:
        return await self._quota.snapshot(tenant_id)

    async def aclose(self) -> None:
        await self._quota.aclose()
