"""查询规划节点：把复合问题拆成可独立检索的子查询（Query Planner）。

多跳问题的召回缺口不是 top_k 不够，而是**检索分布**问题：
"培训审批 + 报销时限"一条查询，训练制度分块天然排在前面，
报销制度里的时限分块排不进 top_k。拆成两条子查询各自检索再融合，
每类信息点都能以自己的关键词参赛（对齐业界 Query Planner 实践）。

两道防线控制成本与风险：
    * 启发式闸门：单信息点问题**不发 LLM 调用**直接透传（多数问题都是单点）；
    * 任何失败（解析坏 JSON / 超长 / 空数组）都回退为原查询——
      规划是增益项，失败模式必须是"退化成单查询检索"，而不是请求失败。
"""

from __future__ import annotations

import json
import re
import time

from app.clients.model_gateway import ModelGatewayClient
from app.config import Settings
from app.graph.nodes.base import add_error, merge_timing
from app.graph.state import RAGState
from packages.common.logging import get_logger
from packages.contracts import ChatMessage, CompletionRequest
from packages.prompts import get_prompt_registry

logger = get_logger("orchestrator.node.plan")

# 复合问题的低成本信号：precision 优先（宁可漏判也不给单点问题多花一次 LLM 调用）
_COMPOUND_HINTS = re.compile(r"另外|以及|分别|还有|同时|再加上|顺便|[?？].*[?？]")

_ARRAY = re.compile(r"\[.*\]", re.DOTALL)


def looks_compound(query: str) -> bool:
    """是否值得尝试分解。闸门只影响"要不要花一次 LLM 调用"。"""
    return bool(_COMPOUND_HINTS.search(query))


def parse_sub_queries(raw: str, original: str, max_queries: int) -> list[str]:
    """从模型输出里稳健地取回子查询列表；任何异常都回退为原查询。"""
    match = _ARRAY.search(raw)
    if not match:
        return [original]
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return [original]
    if not isinstance(parsed, list):
        return [original]
    cleaned: list[str] = []
    for item in parsed:
        if not isinstance(item, str):
            continue
        text = item.strip().strip("\"“”「」'")
        if text and text not in cleaned:
            cleaned.append(text)
    if not cleaned:
        return [original]
    return cleaned[:max_queries] or [original]


def make_plan_node(model_gateway: ModelGatewayClient, settings: Settings):
    """构造查询规划节点：复合问题拆子查询，单信息点透传（不花 LLM 调用），失败回退原查询。"""

    async def plan(state: RAGState) -> dict:
        started = time.perf_counter()
        query = state.get("query", "")

        # 透传路径：未启用 / 单信息点问题。sub_queries 必须始终有值——
        # retrieve 节点按它决定单查询还是融合检索。
        if not settings.decompose_enabled or not looks_compound(query):
            return merge_timing(state, "plan", started, sub_queries=[query])

        try:
            prompt = get_prompt_registry().render("decompose", "v1", query=query)
            resp = await model_gateway.complete(
                CompletionRequest(
                    messages=[ChatMessage(role="user", content=prompt)],
                    temperature=0.0,
                    max_tokens=256,
                    tenant_id=state.get("tenant_id"),
                )
            )
            sub_queries = parse_sub_queries(resp.answer, query, settings.decompose_max_sub_queries)
            logger.info("查询规划: %r -> %s", query[:40], [q[:24] for q in sub_queries])
            return merge_timing(state, "plan", started, sub_queries=sub_queries)
        except Exception as exc:  # noqa: BLE001 - 增益项失败回退，不能让规划打挂问答
            logger.warning("查询规划失败，回退单查询: %s", exc)
            return merge_timing(
                state,
                "plan",
                started,
                sub_queries=[query],
                errors=add_error(state, f"plan_failed: {exc}"),
            )

    return plan


__all__ = ["make_plan_node", "looks_compound", "parse_sub_queries"]
