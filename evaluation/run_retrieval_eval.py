"""检索层评估（不需要 LLM 裁判，可快速反复跑）。

指标（对应 `开发流程/06-测试与验收方案.md` §5.4）：

| 指标 | 含义 |
| --- | --- |
| Hit Rate@K | Top-K 中至少命中一个期望片段的样本占比 |
| Recall@K | 期望片段被召回的比例（多条期望时取平均） |
| MRR | 第一个期望片段的排名倒数（衡量「排得够不够前」） |

**负样本单独处理**：负样本没有「应召回的 chunk」，因此不参与上述指标；
它们用于诊断「检索在知识库范围外的问题上是否仍然自信」——报告 Top-1 的
稠密/稀疏分数分布，以及**返回非空结果的比例**。

后者受相关性门槛影响：启用 `RELEVANCE_MIN_DENSE` 后，低于门槛的问题会直接
返回空结果（走拒答/转人工），该比例会明显下降（实测 100% → 19.44%）。
门槛的校准过程见 `calibrate_threshold.py`。
注意**只有稠密分可用于校准**：BM25 原始分不可跨查询比较。

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
    positives = [item for item in items if item.expected_chunk_ids]
    negatives = [item for item in items if not item.expected_chunk_ids]

    rows = []
    for item in positives:
        run = run_system(item, top_k=top_k, generate_answer=False, rerank=rerank)

        matched = [cid for cid in run.retrieved_ids if cid in item.expected_chunk_ids]
        first_rank = next(
            (
                rank
                for rank, cid in enumerate(run.retrieved_ids, start=1)
                if cid in item.expected_chunk_ids
            ),
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
                "recall": round(len(matched) / len(item.expected_chunk_ids), 4),
                "first_rank": first_rank,
                "reciprocal_rank": round(1.0 / first_rank, 4) if first_rank else 0.0,
                "mode": run.mode,
                "deduped": (run.debug.get("fused_before_dedupe", 0) or 0)
                - (run.debug.get("fused", 0) or 0),
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

    # 负样本诊断：看检索在「没有答案」的问题上给出多高的分
    negative_diagnostics = []
    for item in negatives:
        run = run_system(item, top_k=top_k, generate_answer=False, rerank=rerank)
        top = (run.debug.get("top") or [{}])[0]
        negative_diagnostics.append(
            {
                "id": item.id,
                "question": item.question,
                "top1_chunk_id": top.get("chunk_id"),
                "top1_dense_score": top.get("dense_score"),
                "top1_sparse_score": top.get("sparse_score"),
                "returned": len(run.retrieved_ids),
            }
        )

    return {
        "summary": summarize(rows),
        "by_type": by_type,
        "failures": [r for r in rows if not r["hit"]],
        "rows": rows,
        "negatives": {
            "count": len(negatives),
            "returned_nonempty_rate": _mean(
                [1.0 if d["returned"] > 0 else 0.0 for d in negative_diagnostics]
            ),
            "mean_top1_dense": _mean(
                [d["top1_dense_score"] for d in negative_diagnostics if d["top1_dense_score"] is not None]
            ),
            "mean_top1_sparse": _mean(
                [d["top1_sparse_score"] for d in negative_diagnostics if d["top1_sparse_score"] is not None]
            ),
            "details": negative_diagnostics,
        },
    }


def print_report(result: dict, top_k: int) -> None:
    summary = result["summary"]
    print(f"\n=== 检索层评估（Top-{top_k}，正样本 {summary['count']} 条）===")
    print(f"  Hit Rate@{top_k}: {summary['hit_rate']:.2%}")
    print(f"  Recall@{top_k}  : {summary['recall']:.2%}")
    print(f"  MRR          : {summary['mrr']:.4f}")

    print("\n  分类别：")
    labels = {"lexical": "专有名词类", "semantic": "语义化类"}
    for kind, stats in result["by_type"].items():
        print(
            f"    {labels.get(kind, kind):<6s} n={stats['count']:<3d} "
            f"hit={stats['hit_rate']:.2%} recall={stats['recall']:.2%} mrr={stats['mrr']:.4f}"
        )

    negatives = result["negatives"]
    print(f"\n  负样本诊断（{negatives['count']} 条，不参与上述指标）：")
    print(f"    返回非空结果比例: {negatives['returned_nonempty_rate']:.2%}")
    print(f"    Top-1 稠密分均值: {negatives['mean_top1_dense']:.4f}")
    print(f"    Top-1 稀疏分均值: {negatives['mean_top1_sparse']:.4f}")
    if get_settings().relevance_min_dense > 0:
        print(
            f"    已启用相关性门槛 RELEVANCE_MIN_DENSE={get_settings().relevance_min_dense}："
            "低于该比例的负样本仍拿到片段，需由生成层拒答兜底"
        )
    else:
        print("    未启用相关性门槛（RELEVANCE_MIN_DENSE=0）：所有问题都会拿到片段，拒答全靠生成层")

    failures = result["failures"]
    if failures:
        print(f"\n  未命中样本（{len(failures)} 条）：")
        for row in failures:
            print(f"    {row['id']} [{row['type']}] {row['question']}")
            print(f"      期望: {row['expected']}")
            print(f"      实际: {row['retrieved']}")
    else:
        print("\n  正样本全部命中 ✅")


def main() -> None:
    parser = argparse.ArgumentParser(description="检索层评估")
    parser.add_argument("--top-k", type=int, default=None, help="取前 K 条（默认用配置 TOP_K）")
    parser.add_argument("--rerank", action="store_true", help="开启重排")
    parser.add_argument("--limit-negatives", type=int, default=None, help="负样本只跑前 N 条")
    parser.add_argument("--no-report", action="store_true", help="不写报告文件")
    args = parser.parse_args()

    setup_logging(get_settings().log_level)
    top_k = args.top_k or get_settings().top_k

    items = load_testset()
    if args.limit_negatives is not None:
        keep, seen = [], 0
        for item in items:
            if item.expected_chunk_ids:
                keep.append(item)
            elif seen < args.limit_negatives:
                keep.append(item)
                seen += 1
        items = keep

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
