"""编排服务：装配依赖、跑图、组装响应。"""

from __future__ import annotations

import time

from app.clients.cache import QueryCache
from app.clients.langfuse import build_tracer, record_stage, trace_chat
from app.clients.model_gateway import ModelGatewayClient
from app.clients.retrieval import RetrievalClient
from app.config import Settings
from app.graph import build_graph
from packages.common.errors import ConfigError
from packages.common.ids import new_id
from packages.common.logging import get_logger
from packages.contracts import ChatRequest, ChatResponse, RetrieveMode
from packages.security import Identity

logger = get_logger("orchestrator.service")


class OrchestratorService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.retrieval = RetrievalClient(settings.retrieval_url, settings.request_timeout)
        self.model_gateway = ModelGatewayClient(
            settings.model_gateway_url, settings.request_timeout
        )
        self.cache = QueryCache(
            settings.redis_url, settings.cache_ttl_seconds, settings.cache_enabled
        )
        self.tracer = build_tracer(
            host=settings.langfuse_host,
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            service=settings.service_name,
        )
        self.graph = build_graph(
            retrieval=self.retrieval,
            model_gateway=self.model_gateway,
            cache=self.cache,
            settings=settings,
        )

    async def chat(self, req: ChatRequest, identity: Identity) -> ChatResponse:
        started = time.perf_counter()
        trace_id = new_id("tr_")
        mode: RetrieveMode = req.mode or self.settings.default_mode()
        top_k = req.top_k or self.settings.top_k

        with trace_chat(
            self.tracer,
            query=req.query,
            user_id=identity.user_id,
            tenant_id=identity.tenant_id,
            mode=mode.value,
            top_k=top_k,
            trace_id=trace_id,
        ) as handle:
            final = await self.graph.ainvoke(
                {
                    "query": req.query,
                    "tenant_id": identity.tenant_id,
                    "department_id": identity.department_id,
                    "user_id": identity.user_id,
                    "roles": list(identity.roles),
                    "top_k": top_k,
                    "mode": mode,
                    "temperature": req.temperature,
                    "trace_id": trace_id,
                    "timings": {},
                    "errors": [],
                }
            )
            timings = dict(final.get("timings") or {})
            handle.update(
                output=dict(
                    refused=bool(final.get("refused")),
                    cached=bool(final.get("cached")),
                    citation_count=len(final.get("citations") or []),
                    errors=list(final.get("errors") or []),
                )
            )
            record_stage(handle, "generate", timings)

        timings["total"] = round((time.perf_counter() - started) * 1000, 1)
        logger.info(
            "chat 完成 trace=%s refused=%s cached=%s citations=%s total=%sms",
            trace_id,
            final.get("refused"),
            final.get("cached"),
            len(final.get("citations") or []),
            timings["total"],
        )
        return ChatResponse(
            answer=final.get("answer", ""),
            citations=list(final.get("citations") or []),
            timings_ms=timings,
            refused=bool(final.get("refused", False)),
            cached=bool(final.get("cached", False)),
            model=final.get("model"),
            trace_id=trace_id,
            # 缓存命中时没有 contexts（缓存只存答案与引用），评测脚本需注意
            contexts=list(final.get("contexts") or []) if req.include_contexts else None,
        )

    async def health(self) -> dict[str, str]:
        details: dict[str, str] = {
            "retrieval_url": self.settings.retrieval_url,
            "model_gateway_url": self.settings.model_gateway_url,
            "rerank_enabled": str(self.settings.rerank_enabled),
            "rewrite_enabled": str(self.settings.rewrite_enabled),
            "cache_enabled": str(self.settings.cache_enabled),
        }
        status = "ok"
        try:
            details["retrieval"] = "ok" if await self.retrieval.ping() else "unreachable"
        except Exception as exc:  # noqa: BLE001
            details["retrieval"] = f"error: {exc}"
        try:
            details["model_gateway"] = "ok" if await self.model_gateway.ping() else "unreachable"
        except Exception as exc:  # noqa: BLE001
            details["model_gateway"] = f"error: {exc}"
        # 只认 "ok" 为健康：不可达（unreachable）与探测异常（error: ...）都算故障。
        # 早先的写法是匹配 "unreachable" 字面量，于是依赖抛异常时状态仍是 ok，
        # 编排层故障对上游完全不可见——健康检查的价值被静默抵消。
        for probe in ("retrieval", "model_gateway"):
            if not details.get(probe, "").startswith("ok"):
                status = "degraded"
                break
        details["status"] = status
        return details

    async def aclose(self) -> None:
        await self.retrieval.aclose()
        await self.model_gateway.aclose()
        await self.cache.aclose()
        self.tracer.flush()


__all__ = ["OrchestratorService", "ConfigError"]
