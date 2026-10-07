"""L2 逐样本明细与样本 id 的对齐。

为什么这条值得测：`per_sample_by_id` 的用途是**定位失分样本**。
如果 id 与分数错位，它会比"没有 id"更坏——人会照着 id 去修一个其实没问题的样本。
所以"对不齐就返回空"这条保护必须被钉住。

背景：RAGAS 0.4 的 `EvaluationDataset.from_list` 只保留它认识的 4 个字段，
`sample_id` 会被丢弃（实测 `features()` 里没有它），
所以 id 只能按数据集顺序配回去，安全性靠行数校验保证。
"""

from __future__ import annotations

from typing import Any

import pytest
from app.ragas_runner import _per_sample_by_id, _to_ragas_rows


class _Frame:
    """最小 DataFrame 替身：ragas_runner 只对它做 len()。"""

    def __init__(self, rows: int) -> None:
        self._rows = rows
        self.columns: list[str] = []

    def __len__(self) -> int:
        return self._rows


def test_to_ragas_rows_carries_sample_id() -> None:
    """sample_id 必须留在我们自己的行里——RAGAS 会把它丢掉。"""
    rows = [
        {
            "sample_id": "travel-01",
            "question": "住宿标准是多少",
            "answer": "五百元",
            "contexts": ["五百元"],
            "reference": "五百元",
        }
    ]
    assert _to_ragas_rows(rows)[0]["sample_id"] == "travel-01"


def test_to_ragas_rows_skips_negative_and_failed_samples() -> None:
    base: dict[str, Any] = {
        "sample_id": "x",
        "question": "q",
        "answer": "a",
        "contexts": ["c"],
        "reference": "r",
    }
    rows = [
        {**base, "sample_id": "neg", "should_refuse": True},
        {**base, "sample_id": "err", "error": "boom"},
        {**base, "sample_id": "empty", "answer": ""},
        {**base, "sample_id": "ok"},
    ]
    assert [r["sample_id"] for r in _to_ragas_rows(rows)] == ["ok"]


def test_per_sample_by_id_aligns_scores_in_dataset_order() -> None:
    result = _per_sample_by_id(_Frame(2), {"faithfulness": [1.0, 0.5]}, ["a", "b"])
    assert result == [
        {"sample_id": "a", "faithfulness": 1.0},
        {"sample_id": "b", "faithfulness": 0.5},
    ]


def test_per_sample_by_id_returns_empty_on_row_count_mismatch() -> None:
    """结果行数与输入条数不一致 -> 可能错位 -> 返回空并告警。"""
    assert _per_sample_by_id(_Frame(1), {"faithfulness": [1.0, 0.5]}, ["a", "b"]) == []


def test_per_sample_by_id_returns_empty_on_score_count_mismatch() -> None:
    assert _per_sample_by_id(_Frame(2), {"faithfulness": [1.0]}, ["a", "b"]) == []


def test_per_sample_by_id_returns_empty_without_scores() -> None:
    """裁判输出全 NaN 时分数列表为空，不能返回一堆 id 配空值。"""
    assert _per_sample_by_id(_Frame(0), {}, []) == []


def test_per_sample_by_id_handles_multiple_metrics() -> None:
    per_sample: dict[str, list[float | None]] = {
        "faithfulness": [1.0, 0.5],
        "context_precision": [1.0, 1.0],
    }
    result = _per_sample_by_id(_Frame(2), per_sample, ["a", "b"])
    assert result[1] == {"sample_id": "b", "context_precision": 1.0, "faithfulness": 0.5}


@pytest.mark.parametrize("bad", [None, "not-a-frame", 3])
def test_per_sample_by_id_survives_broken_frame(bad: object) -> None:
    """frame 异常时不能抛异常——它只是诊断信息，不该让整轮评测失败。

    故意传错类型：这里断言的是"不抛异常"，类型错误属于调用方 bug。
    """
    assert _per_sample_by_id(bad, {"faithfulness": [1.0]}, ["a"]) == []
