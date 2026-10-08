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
from packages.common.constants import (
    NO_CITE_MARKER,
    NO_CONTEXT_NOTICE,
    REFUSE_MARKER,
    REFUSE_TEXT,
)
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


def strip_no_cite_marker(answer: str) -> tuple[str, bool]:
    """剥离 ``NO_CITE_MARKER``，返回 (净化后的答案, 是否带过该标记)。

    标记存在的含义是：模型明确声明「本次回答不应带引用」（寒暄 / 声明无资料的
    通用回答）。这时 guard 的「没标引用就兜底附 top1」必须让位——否则会出现
    「答案说资料里没有，脚上却挂着一条引用」这种自相矛盾，且用户无从分辨。
    """
    if NO_CITE_MARKER not in (answer or ""):
        return answer or "", False
    cleaned = (answer or "").replace(NO_CITE_MARKER, "")
    return cleaned.strip(), True


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

    曾经试过加一个「答案太短就不兜底」的闸门，用来避免「你好！有什么可以帮你的吗？」
    这类寒暄被挂上制度引用。**已放弃**：长度区分不出寒暄和实质回答——
    「你好！有什么可以帮你的吗」（13 字）与「转正后凭发票报销，上限五百元。」（15 字）
    长度几乎相同，按长度取舍必然一边误伤（实测确实误伤了后者，测试直接挂掉）。
    真正的解法是让检索层能表达「无相关结果」，而这需要标定相关性阈值：
    混合检索的 RRF 分数只反映排名，表达不了相关性，所以现在**没有**可用信号。
    在那之前靠提示词侧的 ``NO_CITE_MARKER`` 降低概率，兜底逻辑保持原样——
    宁可让寒暄偶发挂错引用，也不要在正常问答上丢掉引用。
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
    """构造守卫节点：拒答识别 + 引用映射（顺序来自重排后 hits）+ 可选 LLM 合规审核。

    引用必须**只从** ``state["hits"]`` 派生，绝不对 hits 再排序——否则编号与文档错位。
    """
    async def guard(state: RAGState) -> dict:
        started = time.perf_counter()
        answer = (state.get("answer") or "").strip()
        hits = list(state.get("hits") or [])

        # 0) 先剥离「本回答不应带引用」标记（寒暄 / 声明无资料的通用回答）。
        #    必须在拒答识别**之前**：这类回答里常含"没有检索到相关内容"之类表述，
        #    先剥离才不会在下一步被当成拒答而丢掉内容。
        answer, marked_no_cite = strip_no_cite_marker(answer)

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

        # 2) 引用映射。模型显式声明过"本次不该带引用"时，跳过兜底：
        #    寒暄或"声明无资料"的回答挂上 top1 引用，是自相矛盾且会误导用户。
        errors = list(state.get("errors") or [])
        if marked_no_cite:
            logger.info("回答声明不附引用（寒暄/无资料通用回答），跳过引用兜底")
            errors = add_error(state, "no_cite_marker")
            citations, fallback = [], False
            # 「模型明确声明本次不附引用」等价于「本次没有资料支撑」——
            # 必须把���个信号传导成 no_context，否则前端不会显示来源警告。
            # 漏掉这一步的后果：答案里既没有引用、也没有拒答、也没有降级标记，
            # 用户看到的是一段 naked 的断言，完全不知道它来自模型常识。
            no_context = True
        else:
            citations, fallback = build_citations(answer, hits)
            if fallback:
                logger.warning("答案未标注引用，已兜底附 top1")
                errors = add_error(state, "citation_fallback_to_top1")
            no_context = False

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
            no_context=no_context,
            errors=errors,
        )

    return guard


def make_refuse_node(model_gateway: ModelGatewayClient, settings: Settings):
    """无资料 / 生成失败出口。

    两条路走到这里，**处理方式不同**：

    1. ``empty_generation``（检索有结果但生成为空）—— 模型异常或被截断。
       仍走拒答：这时候连"资料都没找到"都说不准，用通用知识回答会掩盖故障。
    2. ``empty_retrieval``（检索为空）—— 按 ``ANSWER_FALLBACK_ENABLED`` 决定：
       开启时走**通用回答**（见 :func:`_fallback_answer`），关闭时保持历史拒答行为。

    为什么检索为空不再直接拒答：知识库是**可选的信息来源**，不该成为回答的闸门。
    用户输入「你好」或「年假多少天」而库里没有，不该回一句冷冰冰的「无法回答」。

    但降级答案必须显式声明来源不来自知识库（见 :data:`NO_CONTEXT_NOTICE`）：
    本项目是制度问答，用户可能把答案当作公司规定去执行。
    """

    async def refuse(state: RAGState) -> dict:
        started = time.perf_counter()
        query = state.get("query", "")
        if state.get("hits"):
            reason = "empty_generation"
            logger.info("生成为空，走拒答出口 query=%r", query[:60])
        elif settings.answer_fallback_enabled:
            logger.info("检索为空，走通用回答 query=%r", query[:60])
            answer = await _fallback_answer(model_gateway, state)
            if answer:
                answer, _ = strip_no_cite_marker(answer)
                # citations 必须空：通用知识没有任何资料支撑，挂引用就是编造。
                return merge_timing(
                    state,
                    "guard",
                    started,
                    answer=answer,
                    citations=[],
                    refused=False,
                    no_context=True,
                    errors=add_error(state, "no_context_fallback"),
                )
            # 降级生成本身失败（模型不可用等）：落回拒答，不能让请求变成 5xx
            logger.warning("通用回答生成失败，回退拒答 query=%r", query[:60])
            reason = "fallback_generation_failed"
        else:
            reason = "empty_retrieval"
            logger.info("检索为空且未开启通用回答，走拒答出口 query=%r", query[:60])
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


async def _fallback_answer(model_gateway: ModelGatewayClient, state: RAGState) -> str:
    """无资料时生成通用回答：让模型区分「寒暄」与「实质问题」。

    寒暄（"你好"）直接友好回应——回一句"知识库里没有相关内容"是荒谬的；
    与公司制度相关但无资料的，才声明来源并给通用知识。

    返回空字符串表示生成失败，调用方回退到拒答。
    """
    registry = get_prompt_registry()
    try:
        prompt = registry.render(
            "rag_fallback",
            "v1",
            query=state.get("query", ""),
            no_context_notice=NO_CONTEXT_NOTICE,
            no_cite_marker=NO_CITE_MARKER,
        )
    except Exception as exc:  # noqa: BLE001 - 模板缺失不应让请求失败
        logger.error("渲染 rag_fallback 提示词失败: %s", exc)
        return ""

    try:
        resp = await model_gateway.complete(
            CompletionRequest(
                messages=[ChatMessage(role="user", content=prompt)],
                # 评测传 0 换可复现，这里沿用同一个值，不另造随机性来源
                temperature=state.get("temperature"),
                max_tokens=512,
                tenant_id=state.get("tenant_id"),
            )
        )
    except Exception as exc:  # noqa: BLE001 - 降级失败由调用方兜底
        logger.warning("通用回答调用失败: %s", exc)
        return ""
    return (resp.answer or "").strip()
