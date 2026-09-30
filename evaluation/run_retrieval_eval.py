"""检索质量评估（不需要 LLM 裁判，可快速反复跑）。

指标（对应 `开发流程/06-测试与验收方案.md` §5.4）：

| 指标 | 含义 |
| --- | --- |
| Hit Rate@K | Top-K 中至少命中一个期望片段的样本占比 |
| Recall@K | 期望片段被召回的比例（多条期望时取平均） |
| MRR | 第一个期望片段的排名倒数（衡量「排得够不够前」） |

并给出首次失效样本清单，便于定位检索短板。

用法：
    python -m evaluation.run_retrieval_eval
    python -m evaluation.run_retrieval_eval --top-k 3 --rerank
"""

import argparse
import json
from datetime import datetime

from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging
from evaluation.common import (
    eval_config_snapshot,
    ensure_report_dir,
    load_testset,
    run_system,
)

logger = get_logger(__name__)


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def evaluate(items, *, top_k: int, rerank: bool | None) -> dict:
    """逐条评估并汇总。"""
    rows = []
    for item in items:
        run = run_system(item, top_k=top_k, generate_answer=False, rerank=rerank)

        matched = [cid for cid in run.retrieved_ids if cid in item.expected_chunk_ids]
        first_rank = next(
            (rank for rank, cid in enumerate(run.retrieved_ids, start=1) if cid in item.expected_chunk_ids),
            0,
        )
        rows.append(
            {
                "id": item.id,
                "type": item.type,
                "question": item.question,
                "expected": item.expected_chunk_ids,
                "retrieved": run.retrieved_ids,
                "matched": matched,
                "hit": bool(matched),
                "recall": round(len(matched) / len(item.expected_chunk_ids), 4)
                if item.expected_chunk_ids
                else 0.0,
                "first_rank": first_rank,
                "reciprocal_rank": round(1.0 / first_rank, 4) if first_rank else 0.0,
                "mode": run.mode,
            }
        )

    def summarize(subset: list[dict]) -> dict:
        return {
            "count": len(subset),
            "hit_rate": _mean([1.0 if r["hit"] else 0.0 for r in subset]),
            "recall": _mean([r["recall"] for r in subset]),
            "mrr": _mean([r["reciprocal_rank"] for r in subset]),
        }

    by_type = {
        kind: summarize([r for r in rows if r["type"] == kind])
        for kind in sorted({r["type"] for r in rows})
    }

    return {
        "summary": summarize(rows),
        "by_type": by_type,
        "failures": [r for r in rows if not r["hit"]],
        "rows": rows,
    }


def print_report(result: dict, top_k: int) -> None:
    summary = result["summary"]
    print(f"\n=== 检索质量评估（Top-{top_k}，共 {summary['count']} 条）===")
    print(f"  Hit Rate@{top_k}: {summary['hit_rate']:.2%}")
    print(f"  Recall@{top_k}  : {summary['recall']:.2%}")
    print(f"  MRR          : {summary['mrr']:.4f}")

    print("\n  分类别：")
    for kind, stats in result["by_type"].items():
        label = "专有名词类" if kind == "lexical" else "语义化类"
        print(
            f"    {label:<6s} n={stats['count']:<3d} "
            f"hit={stats['hit_rate']:.2%} recall={stats['recall']:.2%} mrr={stats['mrr']:.4f}"
        )

    failures = result["failures"]
    if failures:
        print(f"\n  未命中样本（{len(failures)} 条）：")
        for row in failures:
            print(f"    {row['id']} [{row['type']}] {row['question']}")
            print(f"      期望: {row['expected']}")
            print(f"      实际: {row['retrieved']}")
    else:
        print("\n  全部命中 ✅")


def main() -> None:
    parser = argparse.ArgumentParser(description="检索质量评估")
    parser.add_argument("--top-k", type=int, default=None, help="取前 K 条（默认用配置 TOP_K）")
    parser.add_argument("--rerank", action="store_true", help="开启重排")
    parser.add_argument("--no-report", action="store_true", help="不写报告文件")
    args = parser.parse_args()

    setup_logging(get_settings().log_level)
    top_k = args.top_k or get_settings().top_k

    items = load_testset()
    result = evaluate(items, top_k=top_k, rerank=True if args.rerank else None)
    print_report(result, top_k)

    if not args.no_report:
        report = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "top_k": top_k,
            "rerank": bool(args.rerank),
            "config": eval_config_snapshot(),
            **result,
        }
        path = ensure_report_dir() / f"retrieval_{datetime.now():%Y%m%d_%H%M%S}.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n报告已写入: {path}")


if __name__ == "__main__":
    main()
