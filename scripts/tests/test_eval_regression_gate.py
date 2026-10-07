"""回归门禁自身的测试。

为什么门禁需要测试：它是一段"决定 CI 红绿"的代码，而且**失败的代价不对称**——
门禁太松（漏报）会放过真实退化；太紧（误报）会让别人给它加 `|| true` 绕过，
那样门禁就永久失效了。所以两个方向都要钉住。

这里用 subprocess 跑真实 CLI：门禁的价值在"按退出码决定成败"，
只测内部函数测不出这一点。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_eval_regression.py"
BASELINE = REPO_ROOT / "configs" / "eval" / "baseline.json"


def _report(**overrides) -> dict:
    """以冻结基线为基准造一份"一切正常"的报告，再按需覆盖。"""
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    summary = {
        "count": baseline["dataset_size"],
        "positive": 34,
        "negative": 20,
        "leak_count": 0,
        "forbidden_count": 0,
        "ragas": {},
        **{
            k: v
            for k, v in baseline["metrics"].items()
            if k not in ("faithfulness", "context_precision", "context_recall")
        },
    }
    summary["ragas"] = {
        "faithfulness": baseline["metrics"]["faithfulness"],
        "context_precision": baseline["metrics"]["context_precision"],
        "context_recall": baseline["metrics"]["context_recall"],
    }
    summary.update(overrides.pop("summary", {}))
    return {"summary": summary, "samples": []}


def _run(tmp_path: Path, report: dict) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--report", str(path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_baseline_report_passes(tmp_path: Path) -> None:
    result = _run(tmp_path, _report())
    assert result.returncode == 0, result.stdout + result.stderr
    assert "门禁通过" in result.stdout


def test_leak_fails(tmp_path: Path) -> None:
    result = _run(tmp_path, _report(summary={"leak_count": 1}))
    assert result.returncode == 1
    assert "越权泄露" in result.stdout


def test_forbidden_content_fails(tmp_path: Path) -> None:
    """禁用内容（提示注入得逞）与越权同级：出现即失败。"""
    result = _run(tmp_path, _report(summary={"forbidden_count": 2}))
    assert result.returncode == 1
    assert "禁用内容" in result.stdout


def test_missing_invariant_field_fails(tmp_path: Path) -> None:
    """旧报告缺少不变量字段时必须失败，而不是当成 0。

    第一版实现用 ``summary.get(key, 0)``：字段缺失会被静默当成"没问题"，
    于是**用旧版服务跑出来的报告永远绿灯**——门禁被版本差异绕过。
    """
    report = _report()
    del report["summary"]["forbidden_count"]
    result = _run(tmp_path, report)
    assert result.returncode == 1, result.stdout
    assert "没有 forbidden_count 字段" in result.stdout


def test_metric_degradation_fails(tmp_path: Path) -> None:
    result = _run(tmp_path, _report(summary={"hit_at_k": 0.9}))
    assert result.returncode == 1
    assert "hit_at_k" in result.stdout


def test_metric_improvement_passes(tmp_path: Path) -> None:
    result = _run(tmp_path, _report(summary={"mrr": 1.0}))
    assert result.returncode == 0, result.stdout


def test_within_tolerance_passes(tmp_path: Path) -> None:
    """容差内的一条样本翻转不算退化（20 条负样本里 1 条 = 5%）。"""
    result = _run(tmp_path, _report(summary={"false_answer_rate": 0.05}))
    assert result.returncode == 0, result.stdout


def test_partial_run_skips_metric_comparison(tmp_path: Path) -> None:
    """子集跑不比较指标——分母不同、比率不可比。

    这条守卫的实际作用：CI 默认只跑 20 条（控制成本），
    若拿它去比全量基线，hit@k 之类的比率会长期误报，
    而误报的门禁很快就没人看了。
    """
    result = _run(tmp_path, _report(summary={"count": 20, "hit_at_k": 0.5}))
    assert result.returncode == 0, result.stdout
    assert "跳过" in result.stdout
    assert "不可比" in result.stdout


def test_partial_run_still_enforces_invariants(tmp_path: Path) -> None:
    """子集跑也必须检查不变量：越权与注入得逞跟样本数无关。"""
    result = _run(tmp_path, _report(summary={"count": 20, "leak_count": 1}))
    assert result.returncode == 1
    assert "越权泄露" in result.stdout


def test_l2_tolerance_stays_above_measured_judge_noise() -> None:
    """L2 容差必须**高于实测裁判噪声**，否则门禁会随机误报。

    实测（2026-10-08）：同一份答案、同一裁判（deepseek-flash，temperature=0）
    连打 5 次，faithfulness 落在 0.788 ~ 0.840，**极差 0.052**。
    门禁容差曾设为 0.05——比噪声下限还小，于是同一份代码有时过有时不过。

    这条测试的作用是让"把容差调回 0.05 省得误报"变成一次显式失败：
    误报的门禁比没有门禁更糟，人会学会给它加 `|| true`。
    """
    sys.path.insert(0, str(SCRIPT.parent))
    from check_eval_regression import RULES  # noqa: PLC0415

    measured_spread = 0.0521  # 5 次同输入重打分的极差
    for key, where, _direction, tolerance, _reason in RULES:
        if where != "l2":
            continue
        assert tolerance > measured_spread, (
            f"{key} 的容差 {tolerance} 不高于实测裁判噪声 {measured_spread}，门禁会随机误报"
        )
