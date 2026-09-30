"""引用回链校验（citation grounding）。

RAG 最常见的幻觉形态**不是完全编造**，而是「引用看起来有据可查，但原文并不支撑那句话」——
这比纯编造更危险，因为它让人放松警惕。

本模块做两层校验：

1. **引用有效性**：回答里的 `[来源: chunk_id]` 是否真的来自本次检索结果（防伪造引用）；
2. **实体落地**：回答中出现的版本号 / 错误码，是否在被引用的片段原文里出现过
   （防「v1.2.3 写成 v1.2.4」「ERR-4041 写成 ERR-4014」这类数字漂移）。

指标用途：`开发流程/06-测试与验收方案.md` 的「引用回链正确率」。
"""

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from app.core.entities import extract_entities

#: 引用标注：`[来源: chunk_id]`，兼容中文/英文冒号
CITATION_RE = re.compile(r"\[来源\s*[:：]\s*([^\]\s]+)\s*\]")


@dataclass
class CitationReport:
    """一次回答的引用校验结果。"""

    citations: list[str] = field(default_factory=list)
    valid_citations: list[str] = field(default_factory=list)
    invalid_citations: list[str] = field(default_factory=list)
    answer_entities: list[str] = field(default_factory=list)
    ungrounded_entities: list[str] = field(default_factory=list)
    has_citation: bool = False
    ok: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _field(chunk: Any, key: str, default: Any = None) -> Any:
    if isinstance(chunk, dict):
        return chunk.get(key, default)
    return getattr(chunk, key, default)


def extract_citations(answer: str) -> list[str]:
    """抽取回答中的引用 chunk_id（保持出现顺序、去重）。"""
    seen: set[str] = set()
    result: list[str] = []
    for match in CITATION_RE.finditer(answer or ""):
        chunk_id = match.group(1).strip()
        if chunk_id and chunk_id not in seen:
            seen.add(chunk_id)
            result.append(chunk_id)
    return result


def verify_citations(answer: str, chunks: list[Any], *, require_citation: bool = True) -> CitationReport:
    """校验回答的引用是否有效、实体是否落地。

    :param require_citation: 为 True 时「完全没有引用」也判为不通过。
    :return: `CitationReport`，`ok` 表示全部校验通过。
    """
    by_id = {str(_field(chunk, "chunk_id", "")): _field(chunk, "content", "") or "" for chunk in chunks}

    citations = extract_citations(answer)
    valid = [cid for cid in citations if cid in by_id]
    invalid = [cid for cid in citations if cid not in by_id]

    cited_text = "\n".join(by_id[cid] for cid in valid)
    answer_entities = sorted(extract_entities(answer or ""))
    cited_entities = extract_entities(cited_text)
    ungrounded = [entity for entity in answer_entities if entity not in cited_entities]

    ok = not invalid and not ungrounded and (bool(valid) or not require_citation)

    return CitationReport(
        citations=citations,
        valid_citations=valid,
        invalid_citations=invalid,
        answer_entities=answer_entities,
        ungrounded_entities=ungrounded,
        has_citation=bool(citations),
        ok=ok,
    )


#: 拒答判定关键词；命中任一即认为模型走了「资料不足」出口
REFUSAL_PATTERNS: tuple[str, ...] = (
    "无法确定",
    "未找到",
    "没有找到",
    "无法回答",
    "不清楚",
    "无可奉告",
    "资料不足",
    "知识库中未",
    "暂未找到",
)

_REFUSAL_RE = re.compile("|".join(REFUSAL_PATTERNS))


def is_refusal(answer: str) -> bool:
    """判断回答是否属于「拒答/承认不知道」。

    用于统计负样本拒答率（`开发流程/06` §5.5）。
    """
    if not answer or not answer.strip():
        return True
    return bool(_REFUSAL_RE.search(answer))
