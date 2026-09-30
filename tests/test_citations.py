"""引用回链校验与拒答判定的单元测试。"""

import pytest

from app.generation.citations import (
    extract_citations,
    is_refusal,
    verify_citations,
)
from app.schemas.retrieval import Chunk, DocSource


def _chunk(chunk_id: str, content: str) -> Chunk:
    return Chunk(chunk_id=chunk_id, doc_id="doc", content=content, source=DocSource.MANUAL)


CHUNKS = [
    _chunk("manual#2", "ERR-4041 表示令牌已过期。请在客户端执行退出登录后重新登录。"),
    _chunk("T-1001#0", "v1.3.0 将令牌签名算法从 HS256 改为 RS256，旧令牌全部失效。"),
]


# ---------------- 引用抽取 ----------------


def test_extract_citations_dedupes_and_keeps_order():
    answer = "结论一 [来源: manual#2]，结论二 [来源: T-1001#0]，再引一次 [来源: manual#2]"
    assert extract_citations(answer) == ["manual#2", "T-1001#0"]


@pytest.mark.parametrize(
    "answer",
    ["没有引用", "", "[来源:]", "[来源 manual#2]"],
)
def test_extract_citations_returns_empty_for_invalid_formats(answer):
    assert extract_citations(answer) == []


def test_extract_citations_supports_fullwidth_colon():
    assert extract_citations("见 [来源： manual#2 ]") == ["manual#2"]


# ---------------- 回链校验 ----------------


def test_verify_citations_passes_for_grounded_answer():
    answer = "请重新登录 [来源: manual#2]，因为签名算法变更 [来源: T-1001#0]。"
    report = verify_citations(answer, CHUNKS)

    assert report.ok is True
    assert report.valid_citations == ["manual#2", "T-1001#0"]
    assert report.ungrounded_entities == []


def test_verify_citations_flags_fabricated_citation():
    """引用了检索结果里不存在的 chunk_id → 伪造引用。"""
    report = verify_citations("回答 [来源: not-retrieved#9]", CHUNKS)

    assert report.ok is False
    assert report.invalid_citations == ["not-retrieved#9"]


def test_verify_citations_flags_ungrounded_error_code():
    """回答里出现了被引片段中不存在的错误码 → 数字漂移。"""
    answer = "请检查 ERR-9999 日志 [来源: manual#2]"
    report = verify_citations(answer, CHUNKS)

    assert report.ok is False
    assert "ERR-9999" in report.ungrounded_entities


def test_verify_citations_flags_ungrounded_version():
    answer = "该问题在 v9.9.9 已修复 [来源: T-1001#0]"
    report = verify_citations(answer, CHUNKS)

    assert report.ok is False
    assert "v9.9.9" in report.ungrounded_entities


def test_verify_citations_grounded_entity_passes():
    answer = "ERR-4041 表示令牌过期 [来源: manual#2]"
    report = verify_citations(answer, CHUNKS)

    assert report.ok is True
    assert report.answer_entities == ["ERR-4041"]


def test_verify_citations_requires_citation_by_default():
    report = verify_citations("没有任何引用的回答", CHUNKS)

    assert report.ok is False
    assert report.has_citation is False


def test_verify_citations_can_allow_missing_citation():
    report = verify_citations("没有引用的回答", CHUNKS, require_citation=False)

    assert report.ok is True


# ---------------- 拒答判定 ----------------


@pytest.mark.parametrize(
    "answer",
    [
        "根据现有资料无法确定，建议转人工。",
        "知识库中暂未找到与该问题相关的资料。",
        "很抱歉，未找到相关记录。",
        "资料不足，无法回答。",
        "",
        "   ",
    ],
)
def test_is_refusal_detects_refusals(answer):
    assert is_refusal(answer) is True


@pytest.mark.parametrize(
    "answer",
    [
        "请执行退出登录后重新登录即可解决。",
        "ERR-4041 表示令牌已过期，请重新登录。",
    ],
)
def test_is_refusal_returns_false_for_real_answers(answer):
    assert is_refusal(answer) is False
