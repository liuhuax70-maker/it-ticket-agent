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
from app.ragas_runner import (
    _apply_equivalence_rules,
    _per_sample_by_id,
    _to_ragas_rows,
)


class _GenPrompt:
    def __init__(self) -> None:
        self.instruction = "Base statement generator instruction."


class _Prompt:
    def __init__(self) -> None:
        self.instruction = "Base NLI instruction."


class _Metric:
    """最小指标替身：只带等价规则要动到的那两个提示词属性。"""

    def __init__(self, with_prompt: bool = True) -> None:
        self.statement_generator_prompt: _GenPrompt | None = None
        if with_prompt:
            self.nli_statements_prompt = _Prompt()
            self.statement_generator_prompt = _GenPrompt()


def test_equivalence_rules_apply_only_to_faithfulness() -> None:
    """规则只注入 faithfulness——它才是字面匹配假阴性的来源。"""
    metrics = [
        ("faithfulness", _Metric()),
        ("context_precision", _Metric()),
    ]
    _apply_equivalence_rules(metrics)
    assert "Chinese numerals" in metrics[0][1].nli_statements_prompt.instruction
    assert metrics[1][1].nli_statements_prompt.instruction == "Base NLI instruction."


def test_equivalence_rules_preserve_base_instruction() -> None:
    """规则是**追加**而不是替换：默认判据（直接推断）仍然生效。"""
    metric = _Metric()
    _apply_equivalence_rules([("faithfulness", metric)])
    assert metric.nli_statements_prompt.instruction.startswith("Base NLI instruction.")


def test_equivalence_rules_are_idempotent() -> None:
    """加载流程可能跑两次（重试/复用），规则不能叠两层。"""
    metric = _Metric()
    _apply_equivalence_rules([("faithfulness", metric)])
    once = metric.nli_statements_prompt.instruction
    _apply_equivalence_rules([("faithfulness", metric)])
    assert metric.nli_statements_prompt.instruction == once


def test_equivalence_rules_survive_metric_without_prompt() -> None:
    """ragas 版本升级改了属性名时，降级为告警而不是抛异常。"""
    _apply_equivalence_rules([("faithfulness", _Metric(with_prompt=False))])  # 不应抛异常


def test_citation_markers_stripped_from_response() -> None:
    """[1][2] 是我们自己的引用语法，不是答案内容，必须剥掉再送裁判。

    不剥的话拆分器会把「要求来源于引用 [1]」当成一条主张——上下文里
    当然没有"[1]"这个字符串，一条正确的答案因此被判 0（实测 0.5）。
    """
    rows = [
        {
            "sample_id": "x",
            "question": "q",
            "answer": "P1 告警需在五分钟内响应 [1]。",
            "contexts": ["c"],
            "reference": "r",
        }
    ]
    response = _to_ragas_rows(rows)[0]["response"]
    assert response == "P1 告警需在五分钟内响应。"


def test_statement_rules_apply_to_generator_prompt() -> None:
    """拆分规则必须进 statement_generator_prompt——假阴性大头在拆分层。"""
    metric = _Metric()
    _apply_equivalence_rules([("faithfulness", metric)])
    assert metric.statement_generator_prompt is not None
    gen = metric.statement_generator_prompt.instruction
    assert "premises" in gen, "缺少「不带问题前提」规则"
    assert "fragments" in gen, "缺少「不拆碎片」规则"
    # 原指令仍在
    assert gen.startswith("Base statement generator instruction.")


def test_statement_rules_idempotent() -> None:
    metric = _Metric()
    _apply_equivalence_rules([("faithfulness", metric)])
    assert metric.statement_generator_prompt is not None
    once = metric.statement_generator_prompt.instruction
    _apply_equivalence_rules([("faithfulness", metric)])
    assert metric.statement_generator_prompt.instruction == once


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
