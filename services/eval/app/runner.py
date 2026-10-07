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
from app.metrics import compute
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
    logger.info(
        "L1 完成: hit@k=%s 漏答=%s 误答=%s 越权=%s 条",
        l1.hit_at_k,
        l1.false_refusal_rate,
        l1.false_answer_rate,
        l1.leak_count,
    )

    l2: dict[str, Any] | None = None
    want_ragas = settings.ragas_enabled if with_ragas is None else with_ragas
    if want_ragas:
        subset = _stratified_subset(rows, settings.ragas_max_samples)
        try:
            # 惰性导入：没装 eval extra 时 L1 照常可用，只是 L2 报错
            from app.ragas_runner import score

            # RAGAS 是同步阻塞的，放到线程里避免卡住事件循环
            l2 = await asyncio.to_thread(score, subset, settings)
        except Exception as exc:  # noqa: BLE001
            # L2 失败不能吃掉已经算好的 L1
            logger.error("L2（RAGAS）打分失败，仅返回 L1 指标: %s", exc)
            l2 = {"metrics": {}, "judge_model": None, "error": str(exc)}

    return {
        "preflight": pre,
        "l1": l1,
        "l2": l2,
        "rows": rows,
    }


__all__ = ["run"]
