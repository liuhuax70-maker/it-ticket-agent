"""引用映射（citations）单元测试。"""

from __future__ import annotations

from app.graph.nodes.guard import build_citations, extract_citation_indexes

from packages.contracts import SearchHit


def _hits(count: int) -> list[SearchHit]:
    return [
        SearchHit(
            chunk_id=f"d_1:{i}",
            doc_id="d_1",
            text=f"第{i + 1}段原文内容",
            chunk_index=i,
            char_start=i * 10,
            char_end=i * 10 + 6,
            doc_title="员工手册",
            section_path="第三章 福利",
        )
        for i in range(count)
    ]


def test_extract_citation_indexes_dedupes_and_sorts() -> None:
    assert extract_citation_indexes("依据[3]，参见[1]与[1]") == [1, 3]
    assert extract_citation_indexes("没有引用") == []


def test_build_citations_maps_index_to_context_order() -> None:
    citations, fallback = build_citations("结论[2]。", _hits(3))
    assert fallback is False
    assert [c.index for c in citations] == [2]
    assert citations[0].chunk_id == "d_1:1"  # index=2 -> hits[1]
    assert citations[0].char_start == 10


def test_build_citations_ignores_out_of_range_index() -> None:
    citations, fallback = build_citations("结论[9]。", _hits(2))
    # 越界引用不被采信，退化为兜底 top1
    assert fallback is True
    assert [c.index for c in citations] == [1]


def test_build_citations_without_hits_returns_empty() -> None:
    citations, fallback = build_citations("结论[1]。", [])
    assert citations == []
    assert fallback is False


def test_snippet_is_truncated_to_200_chars() -> None:
    hits = _hits(1)
    hits[0].text = "长" * 500
    citations, _ = build_citations("结论[1]。", hits)
    assert len(citations[0].snippet) == 200
