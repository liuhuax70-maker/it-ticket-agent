"""评测报告落盘：同时产出 JSON（机器比对）与 Markdown（人读）。

报告是**可回溯的证据**：只有指标的裸数字无法回答「这次比上次差在哪」，
也无法在指标回落时定位到具体样本。因此每条样本的判定依据都要留下。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.metrics import MetricsReport, format_markdown
from packages.common.logging import get_logger

logger = get_logger("eval.reports")

# 未达到该样本数时，报告里明确标注「统计意义不足」
MIN_SAMPLES_FOR_CONFIDENCE = 30


def summarize(payload: dict[str, Any]) -> dict[str, Any]:
    l1: MetricsReport = payload["l1"]
    l2: dict[str, Any] | None = payload.get("l2")
    latencies = [float(r["latency_ms"]) for r in payload["rows"] if r.get("latency_ms") is not None]
    return {
        "count": l1.count,
        "positive": l1.positive_count,
        "negative": l1.negative_count,
        "hit_at_k": l1.hit_at_k,
        "hit_at_k_ci95": list(l1.hit_at_k_ci95) if l1.hit_at_k_ci95 else None,
        "mrr": l1.mrr,
        "snippet_recall": l1.snippet_recall,
        "citation_coverage": l1.citation_coverage,
        "refusal_accuracy": l1.refusal_accuracy,
        "false_refusal_rate": l1.false_refusal_rate,
        "false_answer_rate": l1.false_answer_rate,
        "leak_count": l1.leak_count,
        "latency_ms_p50": l1.latency_ms_p50,
        "latency_ms_p95": l1.latency_ms_p95,
        "ragas": (l2 or {}).get("metrics") or {},
        "judge_model": (l2 or {}).get("judge_model"),
        "authz_mode": payload["preflight"].get("authz_mode"),
        "confidence": "ok" if l1.count >= MIN_SAMPLES_FOR_CONFIDENCE else "insufficient_samples",
        "latency_ms_avg": round(sum(latencies) / len(latencies), 1) if latencies else None,
    }


def write_report(
    reports_dir: str | Path,
    payload: dict[str, Any],
    *,
    name: str | None = None,
    dataset_path: str | None = None,
) -> Path:
    """落盘 JSON + Markdown，返回 JSON 路径。"""
    directory = Path(reports_dir)
    directory.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    slug = name or "eval"
    target = directory / f"{slug}_{stamp}.json"

    l1: MetricsReport = payload["l1"]
    l2: dict[str, Any] | None = payload.get("l2")
    report = {
        "generated_at": stamp,
        "dataset": dataset_path,
        "summary": summarize(payload),
        "preflight": payload["preflight"],
        "l2": {
            "judge_model": (l2 or {}).get("judge_model"),
            "scored_count": (l2 or {}).get("scored_count"),
            "metrics": (l2 or {}).get("metrics"),
            "error": (l2 or {}).get("error"),
        },
        "l1_detail": l1.model_dump(mode="json"),
        "samples": payload["rows"],
    }

    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    target.write_text(serialized, encoding="utf-8")
    (directory / f"{slug}_latest.json").write_text(serialized, encoding="utf-8")

    markdown = format_markdown(
        l1,
        title=f"评测基线（{stamp}）",
        extra={
            **((l2 or {}).get("metrics") or {}),
            **({"裁判模型": (l2 or {}).get("judge_model", "—")} if l2 else {}),
            **({"L2 错误": (l2 or {}).get("error")} if (l2 or {}).get("error") else {}),
        },
    )
    (directory / f"{slug}_latest.md").write_text(markdown, encoding="utf-8")

    logger.info("评测报告已写入 %s（含 Markdown 版 %s_latest.md）", target, slug)
    return target


__all__ = ["MIN_SAMPLES_FOR_CONFIDENCE", "summarize", "write_report"]
