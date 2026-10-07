"""检索侧排序指标（NDCG@k / 检索侧 MRR）的正确性测试。

这个文件的重点不是"覆盖率"，而是**用手算值钉死几个最容易写错的地方**：

1. DCG 的第 1 位权重必须是 1 而不是 0（简化的 ``1/log2(i+1)`` 会让它变成 0，
   于是"排第一"和"没排前面"得分一样）；
2. 指数增益必须真的起作用——grade 3 排第 2 位应当胜过 grade 2 排第 1 位；
3. 截断必须真的生效——排在 k 之外的命中要被砍掉；
4. 分母为 0 与"结果为 0"必须区分开，否则"没召回到任何相关内容"会被算成 N/A。

之所以要这么较真：NDCG 是一个**手写公式**，没有库替我校验，
算错了不会报错、只会安静地给出一个看起来合理的数。
"""

from __future__ import annotations

import math

import pytest
from app.metrics import (
    RANKING_K,
    MetricsReport,
    _apply_ranking_metrics,
    _docs_with_answer_snippet,
    compute,
    dcg_at_k,
    ndcg_at_k,
)

# ---------------------------------------------------------------- 纯函数


def test_dcg_first_position_weight_is_one() -> None:
    """第 1 位的权重必须是 1.0。

    这是手写 DCG 最常见的 bug：用 ``1/log2(i+1)`` 会让第 1 位权重为 0，
    于是"排在最前"与"排在第 2 位"几乎同分（0 vs 0.63），完全失去区分度。
    """
    assert dcg_at_k([7.0], 1) == 7.0
    assert dcg_at_k([7.0], 5) == 7.0


def test_dcg_uses_standard_log2_weights() -> None:
    """权重序列：1, 1/log2(3), 1/log2(4), ..."""
    gains = [1.0, 1.0, 1.0]
    expected = 1.0 + 1.0 / math.log2(3) + 1.0 / math.log2(4)
    assert dcg_at_k(gains, 3) == pytest.approx(expected)


def test_dcg_truncates_at_k() -> None:
    """k 之外的贡献必须被砍掉。"""
    assert dcg_at_k([1.0, 1.0, 1.0, 1.0, 1.0, 1.0], 3) == dcg_at_k([1.0, 1.0, 1.0], 3)


def test_ndcg_perfect_ranking_is_one() -> None:
    """降序排列即完美排序，NDCG 必须精确为 1.0。"""
    assert ndcg_at_k([3, 2, 0], 5) == 1.0


def test_ndcg_single_relevant_at_rank_three_is_one_half() -> None:
    """单个期望文档排在第 3 位：DCG=7/log2(4)=3.5，IDCG=7，比值 0.5。"""
    assert ndcg_at_k([0, 0, 3], 5, [3]) == pytest.approx(0.5)
    assert ndcg_at_k([0, 0, 3], 5, [3]) == pytest.approx(1.0 / math.log2(4))


def test_ndcg_is_not_mrr_except_at_first_rank() -> None:
    """NDCG **不等于** MRR，只在第 1 名重合。

    这条测试是为了钉住一个我自己写错过的说法。DCG 用对数折线 ``1/log2(i+2)``，
    MRR 用线性折线 ``1/(i+1)``，所以同一个名次下 NDCG 恒大于 MRR。
    我曾在指标文档里写"单期望文档时 NDCG 退化成 MRR"——那是错的。
    """
    for rank in (1, 2, 3, 4, 5):
        grades = [0] * (rank - 1) + [3]
        value = ndcg_at_k(grades, 5, [3])
        assert value is not None
        if rank == 1:
            assert value == pytest.approx(1.0)
        else:
            assert value > 1.0 / rank, f"第 {rank} 名：NDCG 应大于 MRR"


def test_ndcg_exponential_gain_rewards_top_rank_over_grade() -> None:
    """grade 3 排第 2 位必须胜过 grade 2 排第 1 位。

    这是分级相关性的意义所在：用二值增益时两者完全同分（都是"命中"），
    NDCG 就会把「对文档但切错分块」和「对文档且切对分块」混成同一个分数。
    """
    right_doc_second = ndcg_at_k([2, 3], 5, [3, 3])
    right_doc_first = ndcg_at_k([3, 2], 5, [3, 3])
    assert right_doc_first is not None and right_doc_second is not None
    assert right_doc_first > right_doc_second


def test_ndcg_orders_multiple_relevant_docs() -> None:
    """两个期望文档时，先后次序必须影响得分。"""
    correct_order = ndcg_at_k([2, 2], 5, [2, 2])
    reversed_order = ndcg_at_k([0, 2, 2], 5, [2, 2])
    assert correct_order == 1.0
    assert reversed_order is not None and reversed_order < correct_order


def test_ndcg_truncation_punishes_relevant_beyond_k() -> None:
    """命中排在 k 之外时，NDCG@3 必须是 0（不是"低分"，是完全没有贡献）。"""
    grades = [0, 0, 0, 0, 3]
    assert ndcg_at_k(grades, 3, [3]) == 0.0
    assert (ndcg_at_k(grades, 5, [3]) or 0.0) > 0.0


def test_ndcg_returns_none_when_no_grades() -> None:
    """没有召回名次（空序列）是无定义，不是 0。"""
    assert ndcg_at_k([], 5) is None


def test_ndcg_returns_none_when_ideal_is_empty() -> None:
    """理想排序本身没有相关文档（IDCG=0）时返回 None，而不是除以 0。"""
    assert ndcg_at_k([0, 0, 0], 5, []) is None


def test_ndcg_zero_when_nothing_relevant_retrieved() -> None:
    """期望文档存在但一个都没召回：IDCG 非 0，所以结果是 **0.0** 而不是 None。

    这条是 0 与 None 的分界：0 表示"检索失败了"（必须计入分母并拉低指标），
    None 表示"这条样本没资格评"（不进分母）。
    """
    assert ndcg_at_k([0, 0, 0], 5, [3]) == 0.0


# ---------------------------------------------------------------- 分级判定


def test_snippet_matching_attributes_chunk_to_its_document() -> None:
    """片段归属必须按"分块→文档"对应关系判定，而不是全文搜索。"""
    row = {
        "expected_snippets": ["五百元"],
        "contexts": ["住宿标准为四百元", "住宿标准为五百元"],
        "chunk_doc_ids": ["d_old", "d_current"],
    }
    assert _docs_with_answer_snippet(row) == {"d_current"}


def test_snippet_matching_tolerates_missing_chunk_attribution() -> None:
    """``chunk_doc_ids`` 缺失（拒答/缓存命中路径）时不得越界或错位取分块。"""
    row = {"expected_snippets": ["五百元"], "contexts": ["五百元"], "chunk_doc_ids": []}
    assert _docs_with_answer_snippet(row) == set()


def test_snippet_matching_without_declared_snippets() -> None:
    """没声明期望片段就无从判定"含答案分块"，返回空集而不是全集。"""
    row = {"expected_snippets": [], "contexts": ["任何文本"], "chunk_doc_ids": ["d_a"]}
    assert _docs_with_answer_snippet(row) == set()


# ---------------------------------------------------------------- 指标装配


def _row(**overrides: object) -> dict:
    base: dict = {
        "sample_id": "s1",
        "should_refuse": False,
        "expected_doc_ids": ["d_a"],
        "expected_snippets": ["答案"],
        "refused": False,
        "citations": [],
        "contexts": [],
        "chunk_doc_ids": [],
        "retrieved_doc_ids": [],
    }
    base.update(overrides)
    return base


def test_apply_ranking_metrics_scores_retrieval_order() -> None:
    """两条样本的期望文档分别在第 1、第 2 位，均值应是两者的平均。"""
    report = MetricsReport()
    _apply_ranking_metrics(
        report,
        [
            _row(
                retrieved_doc_ids=["d_a", "d_b"],
                contexts=["这里有答案", "无关"],
                chunk_doc_ids=["d_a", "d_b"],
            ),
            _row(
                sample_id="s2",
                retrieved_doc_ids=["d_b", "d_a"],
                contexts=["无关", "这里有答案"],
                chunk_doc_ids=["d_b", "d_a"],
            ),
        ],
    )
    expected = (1.0 + 1.0 / math.log2(3)) / 2
    assert report.ndcg_at_k == pytest.approx(expected, abs=1e-4)
    assert report.retrieval_mrr == pytest.approx((1.0 + 0.5) / 2, abs=1e-4)


def test_apply_ranking_metrics_penalises_wrong_rank() -> None:
    report = MetricsReport()
    _apply_ranking_metrics(
        report,
        [
            _row(
                retrieved_doc_ids=["d_b", "d_a"],
                contexts=["无关", "这里有答案"],
                chunk_doc_ids=["d_b", "d_a"],
            )
        ],
    )
    # 期望文档在第 2 位：DCG=7/log2(3)，IDCG=7
    assert report.ndcg_at_k == pytest.approx(1.0 / math.log2(3), abs=1e-4)
    assert report.retrieval_mrr == 0.5


def test_apply_ranking_metrics_excludes_cache_hits() -> None:
    """缓存命中的行没有检索名次，必须被排除并留下可见的说明。"""
    report = MetricsReport()
    _apply_ranking_metrics(
        report,
        [
            _row(retrieved_doc_ids=["d_a"], contexts=["这里有答案"], chunk_doc_ids=["d_a"]),
            _row(sample_id="cached", cached=True, citations=[{"doc_id": "d_a", "snippet": "答案"}]),
        ],
    )
    assert report.retrieval_mrr == 1.0, "只应统计有检索名次的那一条"
    assert any("缓存命中" in note for note in report.notes), "排除必须留下痕迹，否则分母变化不可见"


def test_apply_ranking_metrics_without_any_retrieval_order() -> None:
    report = MetricsReport()
    _apply_ranking_metrics(report, [_row(cached=True)])
    assert report.ndcg_at_k is None
    assert report.retrieval_mrr is None
    assert any("无法计算" in note for note in report.notes)


def test_apply_ranking_metrics_ignores_samples_without_expected_sources() -> None:
    """没声明期望来源的样本没有"该命中什么"，不该进检索侧分母。"""
    report = MetricsReport()
    _apply_ranking_metrics(report, [_row(expected_doc_ids=[], retrieved_doc_ids=["d_a"])])
    assert report.retrieval_mrr is None


def test_compute_exposes_ranking_metrics() -> None:
    report = compute(
        [
            _row(
                retrieved_doc_ids=["d_a"],
                contexts=["这里有答案"],
                chunk_doc_ids=["d_a"],
                answer="答案 [1]",
                citations=[{"doc_id": "d_a", "snippet": "这里有答案"}],
                latency_ms=100,
            )
        ]
    )
    assert report.ndcg_at_k == 1.0
    assert report.retrieval_mrr == 1.0
    assert report.count == 1


def test_ranking_k_is_five() -> None:
    """k 固定为 5（与默认 top_k 一致）。它必须固定——跟着配置变就不可比了。"""
    assert RANKING_K == 5
