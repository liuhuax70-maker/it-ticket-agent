"""意图识别节点。

MVP 策略：**敏感关键词命中即判为敏感**，触发后续人工审核（见 `开发流程/03-MVP 最小闭环.md` §2.1）。
后续可演进为模型分类或可配置规则引擎（v1.2）。

跳转：intent → retrieve（无论是否敏感都要检索，否则草稿没有依据）。
"""

from app.core.config import get_settings
from app.core.logging import get_logger
from app.graph.state import TicketState
from app.schemas.ticket import Intent

logger = get_logger(__name__)


def detect_intent(query: str, keywords: list[str]) -> tuple[str, list[str]]:
    """返回 `(意图, 命中的关键词)`。"""
    matched = [kw for kw in keywords if kw and kw in query]
    if matched:
        return Intent.SENSITIVE.value, matched
    return Intent.CONSULT.value, []


def intent_node(state: TicketState) -> dict:
    """判断意图，并决定是否需要人工审核。"""
    settings = get_settings()
    query = state.get("query", "")

    intent, matched = detect_intent(query, settings.sensitive_keyword_list)
    need_review = bool(matched) or settings.require_review_for_all

    if matched:
        logger.info("命中敏感关键词 %s，本工单转人工审核: ticket_id=%s", matched, state.get("ticket_id"))
    elif settings.require_review_for_all:
        logger.info("配置要求全部工单人工审核: ticket_id=%s", state.get("ticket_id"))

    return {"intent": intent, "need_review": need_review}
