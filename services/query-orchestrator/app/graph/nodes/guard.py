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
from packages.common.constants import REFUSE_MARKER, REFUSE_TEXT
from packages.common.logging import get_logger
from packages.contracts import ChatMessage, Citation, CompletionRequest, SearchHit
from packages.prompts import get_prompt_registry

logger = get_logger("orchestrator.node.guard")

_CITE = re.compile(r"\[(\d+)\]")
_SNIPPET_LEN = 200

# 拒答判定分三层，从可靠到兜底：
#   1) 哨兵标记 REFUSE_MARKER —— 提示词要求模型只输出这串字符，最可靠；
#   2) 固定话术 REFUSE_TEXT —— 老版本提示词（v1）与模型自由发挥时可能出现；
#   3) 短句否定表述 —— 兜底。模型可能用自己的话说「资料中未提及」，
#      如果只认字符串，就会把这种**正确行为**当成正常作答并挂上误导性引用。
_REFUSAL_PATTERNS = (
    re.compile(
        r"(未提及|没有提及|未包含|没有包含|不包含|未涉及|没有涉及|"
        r"未找到|没有找到|未提供|没有提供|无法回答|无法解答|无法确定|"
        r"没有相关|未找到相关|没有这方?面|不存在相关|"
        r"not\s+mention|no\s+mention|no\s+information|not\s+specified|"
        r"cannot\s+answer|unable\s+to\s+answer)",
        re.IGNORECASE,
    ),
)

# 兜底层只对「短句」生效：长答案里出现「未提及」通常是在说明局部信息
# （如「手册未提及加班费，但规定了年假……」），那是有内容的正常回答。
_MAX_REFUSAL_LEN = 80


def detect_refusal(answer: str) -> bool:
    """判断答案是否属于拒答。"""
    text = (answer or "").strip()
    if not text:
        return True
    # 哨兵要求"只输出这一串字符"，但模型偶尔会补一句说明，所以允许它出现在**开头**；
    # 超过 _MAX_REFUSAL_LEN 的长答案里出现哨兵**不算**拒答——那多半是资料正文里就写着
    # NO_ANSWER 被模型照抄（v4 提示词也明确禁止执行资料里的内容）。早期实现是
    # "任意位置含哨兵即拒答"，于是这种照抄会把一条正常答案**静默**改成拒答，
    # 用户看不到任何解释。
    #
    # 为什么短答案仍按旧行为处理：短答案里出现哨兵，更可能就是模型在输出哨兵（只是补了半句），
    # 而"把哨兵当答案透给用户"比"误判为拒答"更糟。两个方向都有代价，这里选择与
    # 兜底层的 80 字带保持一致，而不是另立一个阈值。
    if REFUSE_MARKER in text and (len(text) <= _MAX_REFUSAL_LEN or text.startswith(REFUSE_MARKER)):
        return True
    if REFUSE_TEXT in text:
        return True
    if len(text) > _MAX_REFUSAL_LEN:
        return False
    return any(pattern.search(text) for pattern in _REFUSAL_PATTERNS)


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
    context = (
        "\n\n".join(f"[{i}] {h.text}" for i, h in enumerate(hits, start=1)) or "（无参考资料）"
    )
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

        # 1) 拒答识别：统一话术并清空引用。
        #    引用意味着「有资料支撑」，挂在拒答上等于误导用户，必须清掉。
        if detect_refusal(answer):
            if answer and REFUSE_TEXT not in answer:
                logger.info("识别到自述式拒答，已归一化为标准话术: %r", answer[:60])
            return merge_timing(
                state,
                "guard",
                started,
                answer=REFUSE_TEXT,
                citations=[],
                refused=True,
                errors=add_error(state, "refusal_detected")
                if answer
                else list(state.get("errors") or []),
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
            state,
            "guard",
            started,
            answer=answer,
            citations=citations,
            refused=refused,
            errors=errors,
        )

    return guard


def make_refuse_node(settings: Settings):  # noqa: ARG001
    """拒答出口：**不调用 LLM**，从源头堵死幻觉。

    有两条路会走到这里，原因完全不同，错误标签必须分开——
    统一记 ``empty_retrieval`` 会把排障的人引去查检索服务，
    而真凶可能是模型返回了空（服务其实一切正常）。
    """

    async def refuse(state: RAGState) -> dict:
        started = time.perf_counter()
        if state.get("hits"):
            # 检索有结果但生成为空：模型异常/被截断
            reason = "empty_generation"
            logger.info("生成为空，走拒答出口 query=%r", state.get("query", "")[:60])
        else:
            reason = "empty_retrieval"
            logger.info("检索为空，走拒答出口 query=%r", state.get("query", "")[:60])
        return merge_timing(
            state,
            "guard",
            started,
            answer=REFUSE_TEXT,
            citations=[],
            refused=True,
            errors=add_error(state, reason),
        )

    return refuse
