"""评测编排：前置检查 -> 采集 -> L1 确定性指标 -> （可选）L2 RAGAS。

顺序是刻意的：**L1 永远先算完**。它是免费、可复现、能直接定位到样本的那一层，
即使裁判模型不可用（没装 ragas、本地 Ollama 挂了），基线也应该能产出。
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.collector import _missing_sources_error, collect, preflight
from app.config import Settings
from app.datasets import GoldenSample
from app.metrics import MetricsReport, compute
from packages.common.errors import ValidationError
from packages.common.logging import get_logger

logger = get_logger("eval.runner")


def _stratified_subset(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """按标签分层抽取 L2 样本。

    直接取前 N 条会让 L2 只覆盖评测集开头那一类问题（通常是事实题），
    于是"答案质量"分数根本无法代表整体——这是评测最常见的自欺方式之一。
    这里按第一个标签轮转抽取，保证覆盖面。
    """
    candidates = [
        row
        for row in rows
        if not row.get("should_refuse") and not row.get("error") and row.get("answer")
    ]
    if limit <= 0 or len(candidates) <= limit:
        return candidates

    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in candidates:
        key = str((row.get("tags") or ["untagged"])[0])
        buckets.setdefault(key, []).append(row)

    picked: list[dict[str, Any]] = []
    while len(picked) < limit and buckets:
        for key in sorted(buckets):
            if len(picked) >= limit:
                break
            bucket = buckets.get(key)
            if not bucket:
                buckets.pop(key, None)
                continue
            picked.append(bucket.pop(0))
    return picked


async def _score_l2(rows: list[dict[str, Any]], settings: Settings) -> dict[str, Any] | None:
    """跑 L2（RAGAS）。失败只记录错误，绝不吃掉已经算好的 L1。"""
    subset = _stratified_subset(rows, settings.ragas_max_samples)
    if not subset:
        return None
    try:
        # 惰性导入：没装 eval extra 时 L1 照常可用，只是 L2 报错
        from app.ragas_runner import score

        # RAGAS 是同步阻塞的，放到线程里避免卡住事件循环
        return await asyncio.to_thread(score, subset, settings)
    except Exception as exc:  # noqa: BLE001
        logger.error("L2（RAGAS）打分失败，仅返回 L1 指标: %s", exc)
        return {"metrics": {}, "judge_model": None, "error": str(exc)}


def _log_l1(l1: MetricsReport) -> None:
    """把 L1 核心指标打到日志（hit@k / 漏答 / 误答 / 越权条数 / 禁用内容条数）。"""
    logger.info(
        "L1 完成: hit@k=%s 漏答=%s 误答=%s 越权=%s 条 禁用内容=%s 条",
        l1.hit_at_k,
        l1.false_refusal_rate,
        l1.false_answer_rate,
        l1.leak_count,
        l1.forbidden_count,
    )


async def run(
    samples: list[GoldenSample],
    settings: Settings,
    *,
    with_ragas: bool | None = None,
) -> dict[str, Any]:
    pre = await preflight(samples, settings)
    if pre["missing_sources"] and not settings.allow_missing_corpus:
        raise ValidationError(_missing_sources_error(pre))

    rows = await collect(samples, settings)
    l1 = compute(rows)
    _log_l1(l1)

    want_ragas = settings.ragas_enabled if with_ragas is None else with_ragas
    l2 = await _score_l2(rows, settings) if want_ragas else None

    return {"preflight": pre, "l1": l1, "l2": l2, "rows": rows}


async def rescore(rows: list[dict[str, Any]], settings: Settings) -> dict[str, Any]:
    """只用**已采集**的行重算 L1 + L2（不重新打真实链路）。

    调整裁判模型、指标集合或超时策略时用这个：采集要十几分钟，
    重打分只需要裁判那部分时间——这也是把流程拆成"采集 / 打分"两段的原因。
    """
    l1 = compute(rows)
    _log_l1(l1)
    l2 = await _score_l2(rows, settings) if settings.ragas_enabled else None
    pre = {
        "authz_mode": "rescored-from-cache",
        "missing_sources": [],
        "sample_count": len(rows),
        "identities": sorted({str(row.get("identity", "")) for row in rows}),
    }
    return {"preflight": pre, "l1": l1, "l2": l2, "rows": rows}


__all__ = ["rescore", "run"]
