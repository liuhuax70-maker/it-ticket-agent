"""评测集加载与报告生成测试（不调用任何外部服务）。"""

from __future__ import annotations

import json

import pytest
from app.datasets import SEED_SAMPLES, GoldenSample, ensure_dataset, load_samples
from app.reports import summarize, write_report

from packages.common.errors import ConfigError


def test_ensure_dataset_writes_seed(tmp_path) -> None:
    target = tmp_path / "golden.jsonl"
    assert not target.exists()
    path = ensure_dataset(target)
    assert path.exists()
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == len(SEED_SAMPLES)


def test_ensure_dataset_is_idempotent(tmp_path) -> None:
    target = tmp_path / "golden.jsonl"
    ensure_dataset(target)
    target.write_text('{"question": "自定义问题"}\n', encoding="utf-8")
    ensure_dataset(target)  # 不应覆盖已有数据集
    assert load_samples(target)[0].question == "自定义问题"


def test_load_samples_limit(tmp_path) -> None:
    target = tmp_path / "golden.jsonl"
    ensure_dataset(target)
    assert len(load_samples(target, limit=2)) == 2


def test_load_samples_rejects_malformed_line(tmp_path) -> None:
    target = tmp_path / "bad.jsonl"
    target.write_text('{"question": "ok"}\n{"question": }\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_samples(target)


def test_reference_is_optional() -> None:
    sample = GoldenSample.model_validate(json.loads('{"question": "q"}'))
    assert sample.reference is None
    assert sample.tags == []


def test_summarize_flags_insufficient_samples() -> None:
    payload = {"metrics": {"faithfulness": 0.8}, "count": 5, "refusal_rate": 0.0}
    summary = summarize(payload)
    assert summary["confidence"] == "insufficient_samples"
    assert summary["metrics"]["faithfulness"] == 0.8

    payload["count"] = 50
    assert summarize(payload)["confidence"] == "ok"


def test_write_report_creates_latest_and_timestamped_file(tmp_path) -> None:
    payload = {
        "metrics": {"faithfulness": 0.9},
        "count": 40,
        "judge_model": "chat-test",
        "requested_metrics": ["faithfulness"],
        "per_sample": [{"user_input": "q", "response": "a"}],
    }
    path = write_report(tmp_path, payload, name="ragas", dataset_path="eval_data/golden.jsonl")
    assert path.exists()
    assert (tmp_path / "latest.json").exists()

    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["summary"]["metrics"]["faithfulness"] == 0.9
    assert report["detail"]["judge_model"] == "chat-test"
    assert report["dataset"] == "eval_data/golden.jsonl"
