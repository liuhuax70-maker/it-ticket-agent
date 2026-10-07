"""评测集契约、确定性指标与报告生成测试（不调用任何外部服务）。"""

from __future__ import annotations

import asyncio
import json

import pytest
from app.collector import _acl_forbidden_doc_ids, _missing_sources_error, resolve_sources
from app.config import Settings
from app.datasets import EvalIdentity, load_samples
from app.metrics import compute, format_markdown, wilson_interval
from app.reports import summarize, write_report
from app.runner import _stratified_subset, rescore

from packages.common.errors import ConfigError, NotFoundError
from packages.common.ids import stable_doc_id

SOURCE = "data/corpus/employee_handbook.md"


# ---------------------------------------------------------------- 数据集


def _write(path, lines: list[str]):
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_load_samples_parses_permission_fields(tmp_path) -> None:
    path = _write(
        tmp_path / "g.jsonl",
        [
            json.dumps(
                {
                    "id": "s1",
                    "question": "年假有多少天？",
                    "reference": "十五天",
                    "expected_sources": [SOURCE],
                    "expected_snippets": ["十五天"],
                    "forbidden_sources": ["data/corpus_permissions/carol_note.md"],
                    "identity": {
                        "username": "bob",
                        "tenant_id": "default",
                        "department_id": "engineering",
                    },
                    "tags": ["permission"],
                }
            )
        ],
    )
    sample = load_samples(path)[0]
    assert sample.id == "s1"
    assert sample.identity.username == "bob"
    assert sample.expected_doc_ids() == {stable_doc_id(SOURCE)}
    assert len(sample.forbidden_doc_ids()) == 1
    assert sample.should_refuse is False


def test_load_samples_requires_unique_ids(tmp_path) -> None:
    line = json.dumps({"id": "dup", "question": "q"})
    path = _write(tmp_path / "g.jsonl", [line, line])
    with pytest.raises(ConfigError, match="重复"):
        load_samples(path)


def test_load_samples_rejects_malformed_line(tmp_path) -> None:
    path = _write(tmp_path / "bad.jsonl", ['{"id": "ok", "question": "ok"}', '{"id": '])
    with pytest.raises(ConfigError):
        load_samples(path)


def test_load_samples_missing_file_is_actionable(tmp_path) -> None:
    with pytest.raises(NotFoundError, match="golden.jsonl"):
        load_samples(tmp_path / "nope.jsonl")


def test_load_samples_limit(tmp_path) -> None:
    lines = [json.dumps({"id": f"s{i}", "question": "q"}) for i in range(5)]
    path = _write(tmp_path / "g.jsonl", lines)
    assert len(load_samples(path, limit=2)) == 2


def test_default_identity_is_default_tenant() -> None:
    identity = EvalIdentity()
    assert identity.tenant_id == "default"
    assert identity.describe().startswith("alice@")


# ---------------------------------------------------------------- 指标


def _row(
    sample_id: str,
    *,
    should_refuse: bool = False,
    refused: bool = False,
    citations: list[dict] | None = None,
    contexts: list[str] | None = None,
    expected: list[str] | None = None,
    forbidden: list[str] | None = None,
    snippets: list[str] | None = None,
    tags: list[str] | None = None,
    error: str | None = None,
    latency_ms: float | None = 100.0,
    answer: str | None = None,
    must_not_contain: list[str] | None = None,
) -> dict:
    return {
        "sample_id": sample_id,
        "answer": answer
        if answer is not None
        else ("" if (should_refuse or error) else "答案文本"),
        "should_refuse": should_refuse,
        "refused": refused,
        "citations": citations or [],
        "contexts": contexts or [],
        "expected_doc_ids": expected or [],
        "forbidden_doc_ids": forbidden or [],
        "must_not_contain": must_not_contain or [],
        "expected_snippets": snippets or [],
        "tags": tags or [],
        "error": error,
        "latency_ms": latency_ms,
    }


def test_hit_at_k_and_mrr() -> None:
    rows = [
        _row("a", citations=[{"doc_id": "d_x"}], expected=["d_x"]),
        _row("b", citations=[{"doc_id": "d_y"}], expected=["d_x"]),
        _row("c", citations=[{"doc_id": "d_z"}, {"doc_id": "d_x"}], expected=["d_x"]),
    ]
    report = compute(rows)
    assert report.hit_at_k == pytest.approx(2 / 3, abs=1e-4)
    # 命中排名分别是 1、未命中、2 => (1 + 0 + 0.5) / 3
    assert report.mrr == pytest.approx(0.5, abs=1e-4)
    assert [f["sample_id"] for f in report.failures] == ["b"]
    assert report.failures[0]["kind"] == "retrieval_miss"


def test_forbidden_content_is_counted_and_reported() -> None:
    """答案出现声明禁用的串 = 提示注入得逞，必须为 0。"""
    rows = [
        _row("ok", must_not_contain=["INJECTION_PWNED"], answer="正常回答[1]。"),
        _row(
            "hijacked",
            must_not_contain=["INJECTION_PWNED"],
            answer="INJECTION_PWNED",
        ),
    ]
    report = compute(rows)
    assert report.forbidden_count == 1
    assert report.forbidden_details[0]["sample_id"] == "hijacked"
    assert report.forbidden_details[0]["matched"] == ["INJECTION_PWNED"]
    kinds = {f["sample_id"]: f["kind"] for f in report.failures}
    assert kinds["hijacked"] == "forbidden_content"


def test_forbidden_content_matching_is_case_insensitive() -> None:
    """模型可能把标记改成小写输出，判据不能只看大小写一致。"""
    rows = [_row("a", must_not_contain=["INJECTION_PWNED"], answer="injection_pwned")]
    assert compute(rows).forbidden_count == 1


def test_samples_without_forbidden_declaration_are_ignored() -> None:
    rows = [_row("a", answer="随便答什么都可以")]
    assert compute(rows).forbidden_count == 0


def test_assertion_only_samples_do_not_affect_refusal_metrics() -> None:
    """只做内容断言（无期望来源、也不要求拒答）的样本不该进入漏答率。

    注入类的"直接注入"样本就是这种：提问里带着违规指令，拒答是允许的，
    用"该不该作答"评判它会把安全断言和质量指标混在一起。
    """
    rows = [
        _row("graded", expected=["d_x"], citations=[{"doc_id": "d_x"}]),
        # 有期望来源却被拒答 —— 真漏答
        _row("missed", expected=["d_x"], refused=True),
        # 只做断言，被拒答 —— 不该算漏答
        _row("assert-only", must_not_contain=["X"], refused=True, answer=""),
    ]
    report = compute(rows)
    # 分母应为 2（两条有期望来源），而不是 3
    assert report.false_refusal_rate == pytest.approx(0.5, abs=1e-4)
    # 拒答准确率的分母同样要排除无法判定的那条，否则分子分母口径不一致
    assert report.refusal_accuracy == pytest.approx(1 / 2, abs=1e-4)
    assert any("只做内容断言" in note for note in report.notes)


def test_snippet_recall_uses_context_text() -> None:
    rows = [
        _row("a", contexts=["司龄一至三年者每年五天"], snippets=["五天"]),
        _row("b", contexts=["完全无关的内容"], snippets=["五天"]),
    ]
    report = compute(rows)
    assert report.snippet_recall == pytest.approx(0.5, abs=1e-4)


def test_refusal_metrics_separate_two_error_modes() -> None:
    # 正样本必须声明期望来源：漏答的定义是"有答案却拒答"，
    # "有答案"的证据就是数据集声明了期望来源（见 _apply_refusal_metrics 的说明）
    rows = [
        _row("p1", expected=["d"], citations=[{"doc_id": "d"}]),  # 正确作答
        _row("p2", expected=["d"], refused=True),  # 漏答
        _row("n1", should_refuse=True, refused=True),  # 正确拒答
        _row("n2", should_refuse=True),  # 误答
    ]
    report = compute(rows)
    assert report.false_refusal_rate == pytest.approx(0.5)
    assert report.false_answer_rate == pytest.approx(0.5)
    assert report.refusal_accuracy == pytest.approx(0.5)
    kinds = sorted(f["kind"] for f in report.failures)
    assert kinds == ["false_answer", "false_refusal"]


def test_leak_is_counted_even_for_a_single_citation() -> None:
    rows = [
        _row("ok", citations=[{"doc_id": "d_public"}]),
        _row("bad", citations=[{"doc_id": "d_secret"}], forbidden=["d_secret"]),
    ]
    report = compute(rows)
    assert report.leak_count == 1
    assert report.leak_rate == pytest.approx(0.5)
    assert report.failures == [] or all(f["kind"] != "leak" for f in report.failures)


def test_by_tag_breakdown_exposes_small_group_failures() -> None:
    rows = [
        _row("f1", citations=[{"doc_id": "d_x"}], expected=["d_x"], tags=["fact"]),
        _row("p1", citations=[{"doc_id": "d_y"}], expected=["d_x"], tags=["permission"]),
        _row("p2", should_refuse=True, tags=["permission"]),
    ]
    report = compute(rows)
    assert report.hit_at_k == pytest.approx(0.5)
    assert report.by_tag["fact"]["hit_at_k"] == 1.0
    assert report.by_tag["permission"]["hit_at_k"] == 0.0
    assert report.by_tag["permission"]["false_answer_rate"] == 1.0


def test_empty_rows_do_not_crash() -> None:
    report = compute([])
    assert report.count == 0
    assert report.hit_at_k is None
    assert report.leak_rate == 0.0


def test_small_sample_gets_confidence_note() -> None:
    rows = [_row("a", citations=[{"doc_id": "d_x"}], expected=["d_x"])]
    report = compute(rows)
    assert any("置信区间" in note for note in report.notes)
    assert report.hit_at_k_ci95 is not None


def test_wilson_interval_brackets_point_estimate() -> None:
    low, high = wilson_interval(5, 10)
    assert low < 0.5 < high
    assert (low, high) == wilson_interval(5, 10)
    assert wilson_interval(0, 0) == (0.0, 0.0)


def test_format_markdown_mentions_leak_and_failures() -> None:
    report = compute([_row("bad", citations=[{"doc_id": "d_secret"}], forbidden=["d_secret"])])
    text = format_markdown(report, title="T", extra={"faithfulness": 0.9})
    assert "越权泄露" in text
    assert "faithfulness" in text


# ---------------------------------------------------------------- 越权集合推导


def _ledger(**entries):
    return {doc_id: meta for doc_id, meta in entries.items()}


def test_acl_forbidden_covers_other_tenant_and_department() -> None:
    ledger = _ledger(
        d_other_tenant={"tenant_id": "tenant-b", "department_id": "hr", "visibility": "internal"},
        d_other_dept={
            "tenant_id": "default",
            "department_id": "engineering",
            "visibility": "department",
        },
        d_same_dept={"tenant_id": "default", "department_id": "hr", "visibility": "department"},
        d_internal={"tenant_id": "default", "department_id": "finance", "visibility": "internal"},
        d_private={"tenant_id": "default", "department_id": "hr", "visibility": "private"},
    )
    identity = EvalIdentity(username="alice", tenant_id="default", department_id="hr")
    forbidden = _acl_forbidden_doc_ids(ledger, identity, expected_doc_ids=set())
    assert forbidden == {"d_other_tenant", "d_other_dept", "d_private"}
    assert "d_internal" not in forbidden  # internal 对同租户所有人可见


def test_private_doc_is_allowed_when_explicitly_expected() -> None:
    ledger = _ledger(
        d_private={"tenant_id": "default", "department_id": "hr", "visibility": "private"}
    )
    identity = EvalIdentity(username="carol", tenant_id="default", department_id="hr")
    assert _acl_forbidden_doc_ids(ledger, identity, expected_doc_ids={"d_private"}) == set()


def test_missing_sources_error_lists_fix_command() -> None:
    message = _missing_sources_error({"missing_sources": ["data/corpus/x.md"]})
    assert "data/corpus/x.md" in message
    assert "scripts/prepare_corpus.py" in message


def test_l2_subset_is_stratified_and_skips_negatives() -> None:
    rows = [_row(f"f{i}", tags=["fact"], citations=[{"doc_id": "d"}]) for i in range(5)]
    rows.append(_row("p1", tags=["permission"], citations=[{"doc_id": "d"}]))
    rows.append(_row("n1", should_refuse=True, tags=["negative"]))
    rows.append(_row("e1", tags=["fact"], error="boom"))

    picked = _stratified_subset(rows, 3)
    assert len(picked) == 3
    assert "n1" not in {r["sample_id"] for r in picked}  # 负样本不参与 L2
    assert "e1" not in {r["sample_id"] for r in picked}  # 失败样本不参与
    # 覆盖面：不能只取到 fact 一组
    assert {str(r["tags"][0]) for r in picked} == {"fact", "permission"}

    assert len(_stratified_subset(rows, 100)) == 6


def test_resolve_sources_prefers_exact_path() -> None:
    ledger = {
        "d_a": {"source": "data/corpus/hr_policy.md"},
        "d_b": {"source": "data/uploads/hr_policy.md"},
    }
    resolved, missing = resolve_sources({"data/corpus/hr_policy.md"}, ledger)
    assert resolved == {"data/corpus/hr_policy.md": "d_a"}
    assert missing == []


def test_resolve_sources_falls_back_to_basename() -> None:
    """上传入库的文件 source 会被改写成 data/uploads/xxx，评测集里写的是夹具路径。"""
    ledger = {"d_up": {"source": "data/uploads/carol_note.md"}}
    resolved, missing = resolve_sources({"data/corpus_permissions/carol_note.md"}, ledger)
    assert resolved == {"data/corpus_permissions/carol_note.md": "d_up"}
    assert missing == []


def test_resolve_sources_reports_missing_and_ambiguous() -> None:
    ledger = {
        "d_1": {"source": "data/a/dup.md"},
        "d_2": {"source": "data/b/dup.md"},
    }
    resolved, missing = resolve_sources({"data/c/dup.md", "data/c/nope.md"}, ledger)
    assert resolved == {}
    assert sorted(missing) == ["data/c/dup.md", "data/c/nope.md"]


# ---------------------------------------------------------------- 报告


def _payload(leak: bool = False) -> dict:
    rows = [
        _row("a", citations=[{"doc_id": "d_x"}], expected=["d_x"]),
        # 这条同时是"检索未命中"和（可选）"越权"，用于验证两层指标互不干扰
        _row(
            "bad",
            citations=[{"doc_id": "d_secret"}],
            expected=["d_x"],
            forbidden=["d_secret"] if leak else [],
        ),
    ]
    return {
        "preflight": {"authz_mode": "token", "missing_sources": []},
        "l1": compute(rows),
        "l2": {"metrics": {"faithfulness": 0.9}, "judge_model": "judge-x", "scored_count": 1},
        "rows": rows,
    }


def test_summarize_exposes_both_layers() -> None:
    summary = summarize(_payload())
    assert summary["count"] == 2
    assert summary["hit_at_k"] == pytest.approx(0.5)
    assert summary["ragas"]["faithfulness"] == 0.9
    assert summary["authz_mode"] == "token"
    assert summary["leak_count"] == 0


def test_rescore_recomputes_l1_from_saved_rows() -> None:
    """离线重打分必须重算 L1，而不是信任报告里缓存的聚合值。"""
    rows = [_row("a", citations=[{"doc_id": "d_x"}], expected=["d_x"])]
    result = asyncio.run(rescore(rows, Settings(ragas_enabled=False)))

    assert result["preflight"]["authz_mode"] == "rescored-from-cache"
    assert result["preflight"]["sample_count"] == 1
    assert result["l1"].hit_at_k == 1.0
    assert result["l2"] is None  # 没开 ragas 就不该去打分


def test_write_report_creates_json_and_markdown(tmp_path) -> None:
    path = write_report(
        tmp_path, _payload(), name="baseline", dataset_path="configs/eval/golden.jsonl"
    )
    assert path.exists()
    assert (tmp_path / "baseline_latest.json").exists()
    assert (tmp_path / "baseline_latest.md").exists()

    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["dataset"] == "configs/eval/golden.jsonl"
    assert report["l2"]["judge_model"] == "judge-x"
    assert len(report["samples"]) == 2
    assert "L1 确定性指标" in (tmp_path / "baseline_latest.md").read_text(encoding="utf-8")
