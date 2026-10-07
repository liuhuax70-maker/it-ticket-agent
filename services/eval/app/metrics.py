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


def _split_by_expectation(rows: list[dict[str, Any]]) -> tuple[list[dict], list[dict]]:
    """按"是否应当拒答"切成 (正样本, 负样本)。"""
    positive = [r for r in rows if not r.get("should_refuse")]
    negative = [r for r in rows if r.get("should_refuse")]
    return positive, negative


def _first_hit_rank(row: dict[str, Any]) -> int | None:
    """首个期望来源在引用列表里的排名（1 起）；未命中返回 None。"""
    expected = set(row.get("expected_doc_ids") or [])
    cited = _cited_doc_ids(row)
    return next((i for i, doc_id in enumerate(cited, start=1) if doc_id in expected), None)


def _unique_doc_ids(row: dict[str, Any]) -> list[str]:
    """去重后的引用文档列表（保持出现顺序）。

    同一篇文档常因多个 chunk 被重复引用，直接打印会出现
    ``['d_a', 'd_b', 'd_a']`` 这种看起来像数据错误的内容，
    也会让失败明细比实际更"严重"。
    """
    seen: dict[str, None] = {}
    for doc_id in _cited_doc_ids(row):
        seen.setdefault(doc_id, None)
    return list(seen)


def _apply_retrieval_metrics(report: MetricsReport, positive: list[dict[str, Any]]) -> set[str]:
    """填 hit@k / MRR / 置信区间，返回命中过的样本 id 集合。

    只统计**声明了期望来源的正样本**——负样本与无期望的样本没有"该命中"的概念，
    混进分母会让指标失真。
    """
    graded = [row for row in positive if row.get("expected_doc_ids")]
    hit_samples: set[str] = set()
    if not graded:
        return hit_samples

    reciprocal_ranks: list[float] = []
    for row in graded:
        rank = _first_hit_rank(row)
        if rank is None:
            reciprocal_ranks.append(0.0)
            continue
        hit_samples.add(str(row.get("sample_id")))
        reciprocal_ranks.append(1.0 / rank)

    report.hit_at_k = _rate(len(hit_samples), len(graded))
    report.hit_at_k_ci95 = tuple(  # type: ignore[assignment]
        round(v, 3) for v in wilson_interval(len(hit_samples), len(graded))
    )
    report.mrr = round(sum(reciprocal_ranks) / len(reciprocal_ranks), 4)
    if len(graded) < 30:
        report.notes.append(f"hit@k 只基于 {len(graded)} 条样本，置信区间很宽，请勿据小差异下结论")
    return hit_samples


def _apply_snippet_recall(report: MetricsReport, positive: list[dict[str, Any]]) -> None:
    """期望原文片段出现在召回上下文里的比例（对切分参数不敏感）。"""
    snippet_hits = 0
    snippet_total = 0
    for row in positive:
        haystack = _contexts_text(row)
        for snippet in row.get("expected_snippets") or []:
            snippet_total += 1
            if snippet and snippet in haystack:
                snippet_hits += 1
    report.snippet_recall = _rate(snippet_hits, snippet_total)


def _apply_citation_coverage(report: MetricsReport, positive: list[dict[str, Any]]) -> None:
    """作答样本里带引用的比例——引用可定位是本项目的硬要求。"""
    answered = [row for row in positive if not row.get("refused")]
    report.citation_coverage = _rate(
        sum(1 for row in answered if row.get("citations")), len(answered)
    )


def _apply_refusal_metrics(
    report: MetricsReport,
    positive: list[dict[str, Any]],
    negative: list[dict[str, Any]],
    total: int,
) -> None:
    """拒答相关的三个比率。

    漏答（正样本被拒答）与误答（负样本作答）是**两种相反的失败**，
    必须分开统计：混成一个"拒答准确率"会掩盖其中一个。
    """
    correct_positive = sum(1 for row in positive if not row.get("refused"))
    correct_negative = sum(1 for row in negative if row.get("refused"))
    report.refusal_accuracy = _rate(correct_positive + correct_negative, total)
    report.false_refusal_rate = _rate(len(positive) - correct_positive, len(positive))
    report.false_answer_rate = _rate(len(negative) - correct_negative, len(negative))


def _apply_leak_metrics(report: MetricsReport, rows: list[dict[str, Any]]) -> None:
    """越权泄露——必须为 0。

    ``forbidden_doc_ids`` 由采集器保证是**完整**集合（数据集显式声明 + 台账 ACL 推导），
    这里只做判定，不再自己算一遍可见集合——两处各算一套正是漏检的来源。
    """
    leaks: list[dict[str, Any]] = []
    for row in rows:
        forbidden = set(row.get("forbidden_doc_ids") or [])
        leaked = [doc_id for doc_id in _unique_doc_ids(row) if doc_id in forbidden]
        if leaked:
            leaks.append(
                {
                    "sample_id": row.get("sample_id"),
                    "identity": row.get("identity"),
                    "leaked_doc_ids": leaked,
                }
            )
    report.leak_count = len(leaks)
    report.leak_rate = _rate(len(leaks), len(rows)) or 0.0
    report.leak_details = leaks


def _apply_operational_metrics(report: MetricsReport, rows: list[dict[str, Any]]) -> None:
    """运行态观测：空上下文率、采集错误数、延迟分位数。"""
    empty_contexts = sum(1 for row in rows if not row.get("contexts") and not row.get("citations"))
    report.context_empty_rate = _rate(empty_contexts, len(rows))
    report.error_count = sum(1 for row in rows if row.get("error"))

    latencies = [float(row["latency_ms"]) for row in rows if row.get("latency_ms") is not None]
    report.latency_ms_p50 = _percentile(latencies, 0.5)
    report.latency_ms_p95 = _percentile(latencies, 0.95)


def _breakdown_by_tag(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """按标签分组统计。

    总量指标会掩盖小分组的塌方——权限切片往往只有 8 条，混在 41 条里看不出来。
    """
    breakdown: dict[str, dict[str, Any]] = {}
    tags = sorted({tag for row in rows for tag in (row.get("tags") or [])})
    for tag in tags:
        group = [row for row in rows if tag in (row.get("tags") or [])]
        group_positive = [row for row in group if not row.get("should_refuse")]
        group_negative = [row for row in group if row.get("should_refuse")]
        graded = [row for row in group_positive if row.get("expected_doc_ids")]
        breakdown[tag] = {
            "count": len(group),
            "hit_at_k": _rate(
                sum(1 for row in graded if _first_hit_rank(row) is not None), len(graded)
            ),
            "false_refusal_rate": _rate(
                sum(1 for row in group_positive if row.get("refused")), len(group_positive)
            ),
            "false_answer_rate": _rate(
                sum(1 for row in group_negative if not row.get("refused")), len(group_negative)
            ),
            "leak_count": sum(
                1
                for row in group
                if set(_cited_doc_ids(row)) & set(row.get("forbidden_doc_ids") or [])
            ),
        }
    return breakdown


def _failure_kind(row: dict[str, Any], hit_samples: set[str]) -> tuple[str, str] | None:
    """判断单条样本的失败类型；没有失败返回 None。

    顺序有意义：采集错误 > 误答 > 漏答 > 检索未命中。一条样本只报第一个命中的原因，
    否则同一条样本会在明细里出现好几行，掩盖真正的优先级。
    """
    sample_id = row.get("sample_id")
    cited = _unique_doc_ids(row)
    if row.get("error"):
        return "error", str(row["error"])
    if row.get("should_refuse") and not row.get("refused"):
        return "false_answer", f"负样本未拒答，引用了 {cited}"
    if not row.get("should_refuse") and row.get("refused"):
        return "false_refusal", "有答案却拒答（漏答）"
    expected = set(row.get("expected_doc_ids") or [])
    if expected and not (set(cited) & expected) and sample_id not in hit_samples:
        return "retrieval_miss", f"期望来源未进 top-k，实际引用 {cited}"
    return None


def _collect_failures(rows: list[dict[str, Any]], hit_samples: set[str]) -> list[dict[str, Any]]:
    """只留能直接行动的失败明细。"""
    failures: list[dict[str, Any]] = []
    for row in rows:
        verdict = _failure_kind(row, hit_samples)
        if verdict is not None:
            kind, detail = verdict
            failures.append({"sample_id": row.get("sample_id"), "kind": kind, "detail": detail})
    return failures


def compute(rows: list[dict[str, Any]]) -> MetricsReport:
    """汇总全部 L1 指标。

    这里只做编排：每一类指标各由一个 ``_apply_*`` / ``_breakdown_*`` 函数负责，
    新增指标时加一个函数并在这里调一行，不要往这个函数里堆分支。
    """
    report = MetricsReport()
    report.count = len(rows)
    if not rows:
        report.notes.append("没有采集到任何样本")
        return report

    positive, negative = _split_by_expectation(rows)
    report.positive_count = len(positive)
    report.negative_count = len(negative)

    hit_samples = _apply_retrieval_metrics(report, positive)
    _apply_snippet_recall(report, positive)
    _apply_citation_coverage(report, positive)
    _apply_refusal_metrics(report, positive, negative, len(rows))
    _apply_leak_metrics(report, rows)
    _apply_operational_metrics(report, rows)
    report.by_tag = _breakdown_by_tag(rows)
    report.failures = _collect_failures(rows, hit_samples)
    return report


def _pct(value: float | None) -> str:
    """比率统一显示为百分比；``None``（样本不足无法计算）显示为破折号。"""
    return "—" if value is None else f"{value * 100:.1f}%"


def _header_lines(report: MetricsReport, title: str) -> list[str]:
    return [
        f"# {title}",
        "",
        f"- 样本总数：{report.count}（正样本 {report.positive_count} / 负样本 {report.negative_count}）",
        f"- 延迟：P50 {report.latency_ms_p50} ms，P95 {report.latency_ms_p95} ms",
    ]


def _metric_table(report: MetricsReport) -> list[str]:
    lines = [
        "",
        "## L1 确定性指标",
        "",
        "| 指标 | 数值 | 说明 |",
        "| --- | --- | --- |",
        f"| hit@k | {_pct(report.hit_at_k)} | 正样本中引用到期望来源的比例 |",
        f"| MRR | {'—' if report.mrr is None else f'{report.mrr:.3f}'} | 首个命中来源的排名倒数均值 |",
        f"| 片段召回 | {_pct(report.snippet_recall)} | 期望原文片段出现在召回上下文中的比例 |",
        f"| 引用覆盖 | {_pct(report.citation_coverage)} | 作答样本中带引用的比例 |",
        f"| 拒答准确率 | {_pct(report.refusal_accuracy)} | 正负样本整体判对比例 |",
        f"| 漏答率 | {_pct(report.false_refusal_rate)} | 正样本被误拒 |",
        f"| 误答率 | {_pct(report.false_answer_rate)} | 负样本未拒答 |",
        f"| **越权泄露** | **{report.leak_count} 条** | 引用到该身份不该看到的文档，必须为 0 |",
    ]
    if report.hit_at_k_ci95:
        low, high = report.hit_at_k_ci95
        lines.append(f"| hit@k 95% 区间 | {low * 100:.1f}% ~ {high * 100:.1f}% | Wilson 区间 |")
    return lines


def _extra_table(extra: dict[str, Any] | None) -> list[str]:
    """L2（RAGAS）指标表。数值型保留 4 位小数，其他按原样输出。"""
    if not extra:
        return []
    lines = ["", "## L2 RAGAS 指标", "", "| 指标 | 数值 |", "| --- | --- |"]
    for key, value in extra.items():
        rendered = f"{value:.4f}" if isinstance(value, float) else str(value)
        lines.append(f"| {key} | {rendered} |")
    return lines


def _tag_table(report: MetricsReport) -> list[str]:
    if not report.by_tag:
        return []
    lines = [
        "",
        "## 分组（总量会掩盖小分组塌方）",
        "",
        "| 标签 | 样本 | hit@k | 漏答率 | 误答率 | 越权 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for tag, stats in sorted(report.by_tag.items()):
        lines.append(
            f"| {tag} | {stats['count']} | {_pct(stats['hit_at_k'])} | "
            f"{_pct(stats['false_refusal_rate'])} | {_pct(stats['false_answer_rate'])} | "
            f"{stats['leak_count']} |"
        )
    return lines


def _leak_table(report: MetricsReport) -> list[str]:
    if not report.leak_details:
        return []
    lines = [
        "",
        "## 越权明细（必须为 0）",
        "",
        "| 样本 | 身份 | 泄露的文档 |",
        "| --- | --- | --- |",
    ]
    for item in report.leak_details:
        lines.append(
            f"| {item.get('sample_id')} | {item.get('identity', '')} | {item.get('leaked_doc_ids')} |"
        )
    return lines


# 失败明细最多渲染 40 条：报告是给人读的，全量渲染会被长表格淹没
# （完整数据在同名 JSON 报告里）
FAILURE_TABLE_LIMIT = 40


def _failure_table(report: MetricsReport) -> list[str]:
    if not report.failures:
        return []
    lines = [
        "",
        f"## 失败明细（{len(report.failures)} 条）",
        "",
        "| 样本 | 类型 | 说明 |",
        "| --- | --- | --- |",
    ]
    for item in report.failures[:FAILURE_TABLE_LIMIT]:
        lines.append(f"| {item['sample_id']} | {item['kind']} | {item['detail']} |")
    remaining = len(report.failures) - FAILURE_TABLE_LIMIT
    if remaining > 0:
        lines.append(f"| … | | 其余 {remaining} 条见 JSON 报告 |")
    return lines


def format_markdown(
    report: MetricsReport, *, title: str = "评测基线", extra: dict[str, Any] | None = None
) -> str:
    """把指标报告渲染成人读的 Markdown。

    每个小节一个 ``_`` 函数，这里只负责按顺序拼装——要加一节就加一个函数加一行调用。
    """
    sections = [
        *_header_lines(report, title),
        *_metric_table(report),
        *_extra_table(extra),
        *_tag_table(report),
        *_leak_table(report),
        *_failure_table(report),
    ]
    if report.notes:
        sections.extend(["", "## 说明", ""])
        sections.extend(f"- {note}" for note in report.notes)
    return "\n".join(sections) + "\n"


__all__ = ["MetricsReport", "compute", "format_markdown", "wilson_interval"]
