"""评测报告落盘。

报告是**可回溯的证据**：包含指标、样本数、裁判模型、数据集路径与每条样本明细。
只有指标的裸数字无法回答「这次比上次差在哪」，也无法在指标回落时定位到具体样本。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common.logging import get_logger

logger = get_logger("eval.reports")

# 未达到该样本数时，报告里明确标注「统计意义不足」
MIN_SAMPLES_FOR_CONFIDENCE = 30


def summarize(payload: dict[str, Any]) -> dict[str, Any]:
    metrics: dict[str, Any] = payload.get("metrics", {}) or {}
    count = int(payload.get("count", 0) or 0)
    return {
        "count": count,
        "metrics": metrics,
        "refusal_rate": payload.get("refusal_rate"),
        "latency_ms_avg": payload.get("latency_ms_avg"),
        "confidence": "ok" if count >= MIN_SAMPLES_FOR_CONFIDENCE else "insufficient_samples",
    }


def write_report(
    reports_dir: str | Path,
    payload: dict[str, Any],
    *,
    name: str | None = None,
    dataset_path: str | None = None,
) -> Path:
    directory = Path(reports_dir)
    directory.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = directory / f"{name or 'ragas'}_{stamp}.json"

    report = {
        "generated_at": stamp,
        "dataset": dataset_path,
        "summary": summarize(payload),
        "detail": {
            "judge_model": payload.get("judge_model"),
            "requested_metrics": payload.get("requested_metrics"),
            "samples": payload.get("per_sample", []),
        },
    }
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    latest = directory / "latest.json"
    latest.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    logger.info("评测报告已写入 %s", target)
    return target
