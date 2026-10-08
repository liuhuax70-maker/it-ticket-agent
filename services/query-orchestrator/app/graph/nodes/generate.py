"""生成节点：调用 model-gateway 生成答案。

注意：送给模型的是**用户原问题**，不是改写后的查询——
改写只服务于检索，把改写结果当问题会让答案答非所问。
"""

from __future__ import annotations

import time

from app.clients.model_gateway import ModelGatewayClient
from app.config import Settings
from app.graph.nodes.base import ms
from app.graph.state import RAGState
from packages.common.logging import get_logger
from packages.contracts import GenerateRequest

logger = get_logger("orchestrator.node.generate")


def make_generate_node(model_gateway: ModelGatewayClient, settings: Settings):  # noqa: ARG001
    """构造生成节点：调用 model-gateway 生成答案。

    ⚠️ 送给模型的是**用户原问题**，不是改写后的查询——改写只服务于检索。
    """
    async def generate(state: RAGState) -> dict:
        started = time.perf_counter()
        req = GenerateRequest(
            query=state.get("query", ""),
            contexts=list(state.get("contexts") or []),
            # 留空则由 model-gateway 用自己的默认温度；评测会显式传 0 换取可复现
            temperature=state.get("temperature"),
            tenant_id=state.get("tenant_id"),
            trace_id=state.get("trace_id"),
        )
        resp = await model_gateway.generate(req)

        timings = dict(state.get("timings") or {})
        # 网关侧耗时（模型真正推理的时间）与外层耗时分别记录，
        # 用于区分「模型慢」还是「网络/编排慢」
        timings["generate"] = resp.timings_ms.get("generate", 0.0)
        timings["generate.gateway_total"] = resp.timings_ms.get("total", 0.0)
        timings["generate.round_trip"] = ms(started)
        return {
            "answer": resp.answer,
            "model": resp.model,
            "timings": timings,
            "errors": list(state.get("errors") or []),
        }

    return generate
