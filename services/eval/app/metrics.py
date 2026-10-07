"""确定性指标（L1）：不依赖 LLM 裁判的那部分评测。

为什么要把这一层单独拎出来：

1. **可信**：命中与否、是否拒答、是否越权，全是可判定的布尔事实，
   不受裁判模型波动影响，可以放心用来做前后对比；
2. **便宜快**：不消耗 token，改一次参数就能重跑一遍；
3. **可定位**：失败样本能直接说清"期望 A 却召回了 B"，
   而 RAGAS 之类的分数只能告诉你"变差了"。

RAGAS（L2）负责答案质量这类需要主观判断的维度，两层互补：
先用 L1 把检索与权限的硬伤修掉，再用 L2 打磨答案质量。

指标定义：
    hit@k            正样本中，引用/上下文里出现过期望来源（doc_id）的比例
    mrr              首个命中期望来源的排名的倒数，均值（越接近 1 说明命中越靠前）
    snippet_recall   期望原文片段出现在召回上下文中的比例（比 doc_id 更宽容，且不受切分参数影响）
    citation_coverage  作答样本中带引用的比例（引用强制是产品约束）
    false_refusal    正样本被误判为拒答（漏答）
    false_answer     负样本没有拒答（幻觉风险）
    leak             引用到了该身份**不该看到**的文档——**必须为 0**。
                     禁止集合由采集器推导（数据集声明 + 台账 ACL），本模块只做判定。

注意 leak 与 false_answer 是**两件事**，不能合并：越权是安全问题（存储层过滤失效），
误答是生成质量问题（模型没拒绝）。实测里 alice 追问他人私有文档时"没拒答但引用的
全是自己有权看的文档"——它该计 false_answer，绝不能计 leak。
"""

from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, Field

from packages.common.logging import get_logger

logger = get_logger("eval.metrics")


def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """比例的 Wilson 置信区间。

    小样本下用正态近似会把 3/40 和 5/40 说得像有真实差异，
    Wilson 区间把这种不确定性显式表达出来。
    """
    if total <= 0:
        return (0.0, 0.0)
    phat = successes / total
    denominator = 1 + z * z / total
    centre = (phat + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(phat * (1 - phat) / total + z * z / (4 * total * total)) / denominator
    return (max(0.0, centre - margin), min(1.0, centre + margin))


class MetricsReport(BaseModel):
    count: int = 0
    positive_count: int = 0
    negative_count: int = 0

    hit_at_k: float | None = None
    hit_at_k_ci95: tuple[float, float] | None = None
    mrr: float | None = None
    snippet_recall: float | None = None
    citation_coverage: float | None = None

    refusal_accuracy: float | None = None
    false_refusal_rate: float | None = None
    false_answer_rate: float | None = None

    leak_rate: float = 0.0
    leak_count: int = 0
    leak_details: list[dict[str, Any]] = Field(default_factory=list)
    context_empty_rate: float | None = None
    error_count: int = 0

    latency_ms_p50: float | None = None
    latency_ms_p95: float | None = None

    by_tag: dict[str, dict[str, Any]] = Field(default_factory=dict)
    failures: list[dict[str, Any]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def _percentile(values: list[float], ratio: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(ratio * (len(ordered) - 1))))
    return round(ordered[index], 1)


def _cited_doc_ids(row: dict[str, Any]) -> list[str]:
    return [str(c.get("doc_id", "")) for c in (row.get("citations") or [])]


def _contexts_text(row: dict[str, Any]) -> str:
    parts = list(row.get("contexts") or [])
    parts.extend(str(c.get("snippet", "")) for c in (row.get("citations") or []))
    return "\n".join(parts)


def _rate(successes: int, total: int) -> float | None:
    return round(successes / total, 4) if total else None


def compute(rows: list[dict[str, Any]]) -> MetricsReport:
    report = MetricsReport()
    report.count = len(rows)
    if not rows:
        report.notes.append("没有采集到任何样本")
        return report

    positive = [r for r in rows if not r.get("should_refuse")]
    negative = [r for r in rows if r.get("should_refuse")]
    report.positive_count = len(positive)
    report.negative_count = len(negative)

    # ---- 检索层（只看有期望来源的正样本）----
    with_expectation = [r for r in positive if r.get("expected_doc_ids")]
    hits = 0
    hit_samples: set[str] = set()
    reciprocal_ranks: list[float] = []
    empty_contexts = 0
    for row in rows:
        if not row.get("contexts") and not row.get("citations"):
            empty_contexts += 1

    for row in with_expectation:
        expected = set(row.get("expected_doc_ids") or [])
        cited = _cited_doc_ids(row)
        rank = next((i for i, doc_id in enumerate(cited, start=1) if doc_id in expected), None)
        if rank is not None:
            hits += 1
            hit_samples.add(str(row.get("sample_id")))
            reciprocal_ranks.append(1.0 / rank)
        else:
            reciprocal_ranks.append(0.0)

    if with_expectation:
        report.hit_at_k = _rate(hits, len(with_expectation))
        report.hit_at_k_ci95 = tuple(round(v, 3) for v in wilson_interval(hits, len(with_expectation)))  # type: ignore[assignment]
        report.mrr = round(sum(reciprocal_ranks) / len(reciprocal_ranks), 4)
        if len(with_expectation) < 30:
            report.notes.append(
                f"hit@k 只基于 {len(with_expectation)} 条样本，置信区间很宽，请勿据小差异下结论"
            )

    # ---- 片段级召回（对切分参数不敏感）----
    snippet_hits = 0
    snippet_total = 0
    for row in positive:
        snippets = row.get("expected_snippets") or []
        if not snippets:
            continue
        haystack = _contexts_text(row)
        for snippet in snippets:
            snippet_total += 1
            if snippet and snippet in haystack:
                snippet_hits += 1
    report.snippet_recall = _rate(snippet_hits, snippet_total)

    # ---- 引用覆盖 ----
    answered = [r for r in positive if not r.get("refused")]
    report.citation_coverage = _rate(sum(1 for r in answered if r.get("citations")), len(answered))

    # ---- 拒答正确性 ----
    correct_pos = sum(1 for r in positive if not r.get("refused"))
    correct_neg = sum(1 for r in negative if r.get("refused"))
    report.refusal_accuracy = _rate(correct_pos + correct_neg, len(rows))
    report.false_refusal_rate = _rate(len(positive) - correct_pos, len(positive))
    report.false_answer_rate = _rate(len(negative) - correct_neg, len(negative))

    # ---- 越权泄露（必须为 0）----
    # forbidden_doc_ids 由采集器保证是**完整**集合：数据集显式声明 + 台账 ACL 推导。
    # 这里只做判定，不再自己算可见集合——两处各算一套正是漏检的来源。
    leaks: list[dict[str, Any]] = []
    for row in rows:
        forbidden = set(row.get("forbidden_doc_ids") or [])
        bad = [d for d in _cited_doc_ids(row) if d in forbidden]
        if bad:
            leaks.append(
                {
                    "sample_id": row.get("sample_id"),
                    "identity": row.get("identity"),
                    "leaked_doc_ids": bad,
                }
            )
    report.leak_count = len(leaks)
    report.leak_rate = _rate(len(leaks), len(rows)) or 0.0
    report.leak_details = leaks

    report.context_empty_rate = _rate(empty_contexts, len(rows))
    report.error_count = sum(1 for r in rows if r.get("error"))

    latencies = [float(r["latency_ms"]) for r in rows if r.get("latency_ms") is not None]
    report.latency_ms_p50 = _percentile(latencies, 0.5)
    report.latency_ms_p95 = _percentile(latencies, 0.95)

    # ---- 分组：总量指标会掩盖权限切片这类小分组的塌方 ----
    tags = sorted({tag for row in rows for tag in (row.get("tags") or [])})
    for tag in tags:
        group = [r for r in rows if tag in (r.get("tags") or [])]
        group_pos = [r for r in group if not r.get("should_refuse")]
        group_neg = [r for r in group if r.get("should_refuse")]
        group_with_expectation = [r for r in group_pos if r.get("expected_doc_ids")]
        group_hits = sum(
            1
            for r in group_with_expectation
            if set(_cited_doc_ids(r)) & set(r.get("expected_doc_ids") or [])
        )
        report.by_tag[tag] = {
            "count": len(group),
            "hit_at_k": _rate(group_hits, len(group_with_expectation)),
            "false_refusal_rate": _rate(
                sum(1 for r in group_pos if r.get("refused")), len(group_pos)
            ),
            "false_answer_rate": _rate(sum(1 for r in group_neg if not r.get("refused")), len(group_neg)),
            "leak_count": sum(
                1
                for r in group
                if set(_cited_doc_ids(r)) & set(r.get("forbidden_doc_ids") or [])
            ),
        }

    # ---- 失败明细：只留能直接行动的 ----
    for row in rows:
        sample_id = row.get("sample_id")
        expected = set(row.get("expected_doc_ids") or [])
        cited = _cited_doc_ids(row)
        if row.get("error"):
            report.failures.append({"sample_id": sample_id, "kind": "error", "detail": row["error"]})
        elif row.get("should_refuse") and not row.get("refused"):
            report.failures.append(
                {
                    "sample_id": sample_id,
                    "kind": "false_answer",
                    "detail": f"负样本未拒答，引用了 {cited}",
                }
            )
        elif not row.get("should_refuse") and row.get("refused"):
            report.failures.append(
                {"sample_id": sample_id, "kind": "false_refusal", "detail": "有答案却拒答（漏答）"}
            )
        elif expected and not (set(cited) & expected) and sample_id not in hit_samples:
            report.failures.append(
                {
                    "sample_id": sample_id,
                    "kind": "retrieval_miss",
                    "detail": f"期望来源未进 top-k，实际引用 {cited}",
                }
            )
    return report


def format_markdown(report: MetricsReport, *, title: str = "评测基线", extra: dict[str, Any] | None = None) -> str:
    def pct(value: float | None) -> str:
        return "—" if value is None else f"{value * 100:.1f}%"

    lines = [
        f"# {title}",
        "",
        f"- 样本总数：{report.count}（正样本 {report.positive_count} / 负样本 {report.negative_count}）",
        f"- 延迟：P50 {report.latency_ms_p50} ms，P95 {report.latency_ms_p95} ms",
        "",
        "## L1 确定性指标",
        "",
        "| 指标 | 数值 | 说明 |",
        "| --- | --- | --- |",
        f"| hit@k | {pct(report.hit_at_k)} | 正样本中引用到期望来源的比例 |",
        f"| MRR | {'—' if report.mrr is None else f'{report.mrr:.3f}'} | 首个命中来源的排名倒数均值 |",
        f"| 片段召回 | {pct(report.snippet_recall)} | 期望原文片段出现在召回上下文中的比例 |",
        f"| 引用覆盖 | {pct(report.citation_coverage)} | 作答样本中带引用的比例 |",
        f"| 拒答准确率 | {pct(report.refusal_accuracy)} | 正负样本整体判对比例 |",
        f"| 漏答率 | {pct(report.false_refusal_rate)} | 正样本被误拒 |",
        f"| 误答率 | {pct(report.false_answer_rate)} | 负样本未拒答 |",
        f"| **越权泄露** | **{report.leak_count} 条** | 出现禁止来源或可见集合之外的文档，必须为 0 |",
    ]
    if report.hit_at_k_ci95:
        lines.append(
            f"| hit@k 95% 区间 | {report.hit_at_k_ci95[0] * 100:.1f}% ~ {report.hit_at_k_ci95[1] * 100:.1f}% | Wilson 区间 |"
        )
    if extra:
        lines.extend(["", "## L2 RAGAS 指标", "", "| 指标 | 数值 |", "| --- | --- |"])
        for key, value in extra.items():
            lines.append(f"| {key} | {'—' if value is None else f'{value:.4f}'} |" if isinstance(value, (int, float)) else f"| {key} | {value} |")

    if report.by_tag:
        lines.extend(["", "## 分组（总量会掩盖小分组塌方）", "", "| 标签 | 样本 | hit@k | 漏答率 | 误答率 | 越权 |", "| --- | --- | --- | --- | --- | --- |"])
        for tag, stats in sorted(report.by_tag.items()):
            lines.append(
                f"| {tag} | {stats['count']} | {pct(stats['hit_at_k'])} | "
                f"{pct(stats['false_refusal_rate'])} | {pct(stats['false_answer_rate'])} | {stats['leak_count']} |"
            )

    if report.leak_details:
        lines.extend(
            ["", "## 越权明细（必须为 0）", "", "| 样本 | 身份 | 泄露的文档 |", "| --- | --- | --- |"]
        )
        for item in report.leak_details:
            lines.append(
                f"| {item.get('sample_id')} | {item.get('identity', '')} | {item.get('leaked_doc_ids')} |"
            )

    if report.failures:
        lines.extend(["", f"## 失败明细（{len(report.failures)} 条）", "", "| 样本 | 类型 | 说明 |", "| --- | --- | --- |"])
        for item in report.failures[:40]:
            lines.append(f"| {item['sample_id']} | {item['kind']} | {item['detail']} |")
        if len(report.failures) > 40:
            lines.append(f"| … | | 其余 {len(report.failures) - 40} 条见 JSON 报告 |")

    if report.notes:
        lines.extend(["", "## 说明", ""])
        lines.extend(f"- {note}" for note in report.notes)
    return "\n".join(lines) + "\n"


__all__ = ["MetricsReport", "compute", "format_markdown", "wilson_interval"]
