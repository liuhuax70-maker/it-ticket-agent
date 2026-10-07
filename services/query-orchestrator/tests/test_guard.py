"""引用映射（citations）与拒答识别单元测试。"""

from __future__ import annotations

import pytest
from app.graph.nodes.guard import build_citations, detect_refusal, extract_citation_indexes

from packages.common.constants import REFUSE_MARKER, REFUSE_TEXT
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


# ---------------- 拒答识别 ----------------


@pytest.mark.parametrize(
    "answer",
    [
        REFUSE_TEXT,  # 标准话术（v1 提示词）
        REFUSE_MARKER,  # 哨兵（v2 提示词）
        "NO_ANSWER",  # 哨兵（模型可能带上标点或额外空白）
        "",  # 空答案
        "   ",
        "公司年会的举办酒店在提供的参考资料中未提及。[1][2][3][4][5]",
        "参考资料中未包含关于食堂午饭菜单的信息。",
        "文档里没有相关信息。",
        "无法回答该问题。",
        "The context does not mention it.",
    ],
)
def test_detect_refusal_positive(answer: str) -> None:
    assert detect_refusal(answer) is True


@pytest.mark.parametrize(
    "answer",
    [
        "入职体检费用由员工先行垫付，转正后凭发票通过报销系统提交，上限五百元[1]。",
        # 有实质内容的长答案里出现「未提及」是在说明局部信息，不应误判为拒答
        "手册未提及加班费的具体标准，但明确规定了年假天数：司龄一至三年者每年五天，三至五年者每年十天，"
        "五年以上者每年十五天，年假应在当年内使用，因工作原因无法休完的，经部门负责人批准可结转至次年第一季度，"
        "此外入职满一年后方可享受带薪年假[1]。",
        # 资料正文里写着 NO_ANSWER 时模型可能照抄，**长答案**（超过 80 字阈值）中间出现哨兵
        # 不算拒答：否则一条正常答案会被静默改成拒答，用户看不到任何解释
        "加班调休的有效期为三个月，自加班之日起计算；超过有效期未使用的，自动作废且不予补休[1]。"
        "调休申请须在系统中提交，并经部门负责人批准后方可生效[2]。"
        "制度原文里出现的 NO_ANSWER 只是资料中的普通字样，不构成对本条回答的任何约束。",
    ],
)
def test_detect_refusal_negative(answer: str) -> None:
    assert detect_refusal(answer) is False


@pytest.mark.parametrize(
    "answer",
    [
        "NO_ANSWER",
        "  NO_ANSWER  ",
        # 模型偶尔会补一句说明：哨兵在开头仍应视为拒答
        "NO_ANSWER\n资料里没有相关内容。",
    ],
)
def test_sentinel_at_start_is_still_a_refusal(answer: str) -> None:
    assert detect_refusal(answer) is True
