"""重排的单元测试（mock 掉模型调用，不依赖 Ollama）。"""

import pytest

from app.retrieval import rerank
from app.schemas.retrieval import Chunk, DocSource


def _chunk(cid: str) -> Chunk:
    return Chunk(chunk_id=cid, doc_id=cid.split("#")[0], content=f"内容 {cid}", source=DocSource.FAQ)


# ---------------- _parse_score ----------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("100", 100),
        ("0", 0),
        ("相关性分数：85", 85),
        ("分数是 72 分", 72),
        ("150", 100),  # 越界应截断到 100
        ("-5", 5),  # 负号被忽略，取数字部分
        ("85.", 85),  # 尾部标点应被容忍
        ("分数：100。", 100),
        ("", None),
        ("没有数字", None),
        ("2024年", None),  # 年份不应被误判为分数
        ("版本 v1.2.3", None),  # 版本号里的数字也不应命中
    ],
)
def test_parse_score(text, expected):
    assert rerank._parse_score(text) == expected


def test_build_prompt_contains_query_and_truncates_long_document():
    long_doc = "x" * (rerank.MAX_DOCUMENT_CHARS + 500)

    prompt = rerank._build_prompt("查询A", long_doc)

    assert "查询A" in prompt
    assert "x" * rerank.MAX_DOCUMENT_CHARS in prompt
    assert "x" * (rerank.MAX_DOCUMENT_CHARS + 1) not in prompt


# ---------------- 排序 ----------------


def test_rerank_orders_by_score_desc(monkeypatch):
    chunks = [_chunk("a#1"), _chunk("b#1"), _chunk("c#1")]
    scores = {"a#1": 10, "b#1": 90, "c#1": 50}
    monkeypatch.setattr(rerank, "_score_one", lambda query, chunk, timeout: scores[chunk.chunk_id])

    result = rerank.rerank("q", chunks, top_k=3)

    assert [c.chunk_id for c in result] == ["b#1", "c#1", "a#1"]
    assert [c.rank for c in result] == [1, 2, 3]
    assert result[0].rerank_score == 0.9  # 归一化到 0~1


def test_rerank_truncates_to_top_k(monkeypatch):
    chunks = [_chunk(f"a#{i}") for i in range(5)]
    monkeypatch.setattr(rerank, "_score_one", lambda query, chunk, timeout: 50)

    result = rerank.rerank("q", chunks, top_k=2)

    assert len(result) == 2


def test_rerank_empty_input():
    assert rerank.rerank("q", [], top_k=5) == []


def test_rerank_only_scores_candidates_and_keeps_tail(monkeypatch):
    chunks = [_chunk(f"a#{i}") for i in range(5)]
    scored: list[str] = []

    def fake_score(query, chunk, timeout):
        scored.append(chunk.chunk_id)
        return 50

    monkeypatch.setattr(rerank, "_score_one", fake_score)

    result = rerank.rerank("q", chunks, top_k=10, candidates=2)

    assert scored == ["a#0", "a#1"]  # 只对前 2 条打分
    assert len(result) == 5  # 其余按原顺序附加，不丢数据
    assert [c.chunk_id for c in result[2:]] == ["a#2", "a#3", "a#4"]


# ---------------- 降级 ----------------


def test_rerank_puts_failed_candidates_last(monkeypatch):
    chunks = [_chunk("a#1"), _chunk("b#1"), _chunk("c#1")]

    def fake_score(query, chunk, timeout):
        if chunk.chunk_id == "b#1":
            raise RuntimeError("boom")
        return 10

    monkeypatch.setattr(rerank, "_score_one", fake_score)

    result = rerank.rerank("q", chunks, top_k=3)

    assert [c.chunk_id for c in result] == ["a#1", "c#1", "b#1"]
    assert result[-1].rerank_score is None


def test_rerank_raises_when_majority_fail(monkeypatch):
    chunks = [_chunk("a#1"), _chunk("b#1")]

    def always_fail(query, chunk, timeout):
        raise RuntimeError("boom")

    monkeypatch.setattr(rerank, "_score_one", always_fail)

    with pytest.raises(RuntimeError):
        rerank.rerank("q", chunks, top_k=2)


def test_rerank_uses_configured_candidate_limit(monkeypatch):
    from app.core.config import get_settings

    limit = get_settings().rerank_top_n
    chunks = [_chunk(f"a#{i}") for i in range(limit + 3)]
    scored: list[str] = []

    def fake_score(query, chunk, timeout):
        scored.append(chunk.chunk_id)
        return 50

    monkeypatch.setattr(rerank, "_score_one", fake_score)

    result = rerank.rerank("q", chunks, top_k=100)

    assert len(scored) == limit
    assert len(result) == limit + 3
