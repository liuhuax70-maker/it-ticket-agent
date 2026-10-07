"""守卫节点：引用映射、拒答出口、可选 LLM 合规审核。

引用映射的硬约束（旧 P0 坑位 #8）：
    contexts 的顺序 == 送入 prompt 的编号顺序 == citations.index 的顺序。
因此这里**只从 state["hits"]（重排后的最终顺序）派生引用**，
绝不对 hits 再排一次序。
"""

from __future__ import annotations

import json
import re
import time

from app.clients.model_gateway import ModelGatewayClient
from app.config import Settings
from app.graph.nodes.base import add_error, merge_timing
from app.graph.state import RAGState
from packages.common.constants import REFUSE_TEXT
from packages.common.logging import get_logger
from packages.contracts import ChatMessage, Citation, CompletionRequest, SearchHit
from packages.prompts import get_prompt_registry

logger = get_logger("orchestrator.node.guard")

_CITE = re.compile(r"\[(\d+)\]")
_SNIPPET_LEN = 200


def extract_citation_indexes(answer: str) -> list[int]:
    """从答案中抽取 [n] 编号（去重、升序）。"""
    return sorted({int(m) for m in _CITE.findall(answer or "")})


def build_citations(answer: str, hits: list[SearchHit]) -> tuple[list[Citation], bool]:
    """把答案里的 [n] 映射为 citations。

    Returns:
        (citations, 是否发生了兜底)
        模型漏标引用时兜底附上 top1 —— 「引用非空」是接口契约，
        但兜底必须在 errors 里留痕，避免把兜底当成正常行为。
    """
    if not hits:
        return [], False

    used = [i for i in extract_citation_indexes(answer) if 1 <= i <= len(hits)]
    fallback = not used
    picked = used or [1]

    citations: list[Citation] = []
    for index in picked:
        hit = hits[index - 1]
        citations.append(
            Citation(
                index=index,
                chunk_id=hit.chunk_id,
                doc_id=hit.doc_id,
                doc_title=hit.doc_title,
                chunk_index=hit.chunk_index,
                section_path=hit.section_path,
                char_start=hit.char_start,
                char_end=hit.char_end,
                score=hit.score,
                snippet=hit.text[:_SNIPPET_LEN],
            )
        )
    return citations, fallback


async def _llm_grounded(
    model_gateway: ModelGatewayClient, state: RAGState, answer: str
) -> tuple[bool, str]:
    """LLM 合规审核：判断答案是否完全由上下文支撑。解析失败时按**通过**处理。

    解析失败不拒绝，是为了避免一个不稳定的判官把正确回答打成拒答；
    审核开启与否由 ``GUARD_LLM_ENABLED`` 决定，默认关闭。
    """
    hits = list(state.get("hits") or [])
    context = "\n\n".join(f"[{i}] {h.text}" for i, h in enumerate(hits, start=1)) or "（无参考资料）"
    prompt = get_prompt_registry().render(
        "guard", "v1", context=context, answer=answer, context_count=len(hits)
    )
    resp = await model_gateway.complete(
        CompletionRequest(
            messages=[ChatMessage(role="user", content=prompt)],
            temperature=0.0,
            max_tokens=256,
            tenant_id=state.get("tenant_id"),
        )
    )
    raw = resp.answer.strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("合规审核返回非 JSON，按通过处理: %r", raw[:120])
        return True, "unparsed"
    grounded = bool(payload.get("grounded", True))
    return grounded, str(payload.get("reason", ""))


def make_guard_node(model_gateway: ModelGatewayClient, settings: Settings):
    async def guard(state: RAGState) -> dict:
        started = time.perf_counter()
        answer = (state.get("answer") or "").strip()
        hits = list(state.get("hits") or [])

        # 1) 模型自己给出了拒答话术 -> 视为拒答，且不带引用
        if not answer or REFUSE_TEXT in answer:
            return merge_timing(
                state,
                "guard",
                started,
                answer=answer or REFUSE_TEXT,
                citations=[],
                refused=True,
            )

        # 2) 引用映射
        citations, fallback = build_citations(answer, hits)
        errors = list(state.get("errors") or [])
        if fallback:
            logger.warning("答案未标注引用，已兜底附 top1")
            errors = add_error(state, "citation_fallback_to_top1")

        # 3) 可选 LLM 合规审核（默认关闭）
        refused = False
        if settings.guard_llm_enabled and hits:
            try:
                grounded, reason = await _llm_grounded(model_gateway, state, answer)
                if not grounded:
                    logger.warning("合规审核判定答案不被支撑: %s", reason)
                    return merge_timing(
                        state,
                        "guard",
                        started,
                        answer=REFUSE_TEXT,
                        citations=[],
                        refused=True,
                        errors=[*errors, f"guard_ungrounded: {reason}"],
                    )
            except Exception as exc:  # noqa: BLE001 - 审核失败不阻断主链路
                logger.error("合规审核调用失败，按通过处理: %s", exc)
                errors = [*errors, f"guard_failed: {exc}"]

        return merge_timing(
            state, "guard", started, answer=answer, citations=citations, refused=refused, errors=errors
        )

    return guard


def make_refuse_node(settings: Settings):  # noqa: ARG001
    """检索为空时的拒答出口：**不调用 LLM**，从源头堵死幻觉。"""

    async def refuse(state: RAGState) -> dict:
        started = time.perf_counter()
        logger.info("检索为空，走拒答出口 query=%r", state.get("query", "")[:60])
        return merge_timing(
            state,
            "guard",
            started,
            answer=REFUSE_TEXT,
            citations=[],
            refused=True,
            errors=add_error(state, "empty_retrieval"),
        )

    return refuse
