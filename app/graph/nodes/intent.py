"""意图识别节点。

MVP 策略：敏感关键词 / 规则命中即判为 sensitive，触发人工审核；
后续演进为模型分类（见 `开发流程/03-MVP 最小闭环.md` §2.1）。
"""

from app.graph.state import TicketState

# 骨架占位关键词，实际以可配置的敏感词表替代
SENSITIVE_KEYWORDS: tuple[str, ...] = ()


def intent_node(state: TicketState) -> dict:
    """返回意图与是否需人工审核。

    跳转：need_review=True → draft_node；否则 → retrieve_node。
    """
    # TODO(后续)：规则/模型识别意图
    raise NotImplementedError("骨架占位：intent_node 将在后续编码阶段实现")
