"""常数可追溯：`top_k` 扫参。

要点文档 1.3 / 3.3①：`top_k=5` 如果不是测出来的，就答不出「为什么是 5 不是 8」。
本脚本固定其他变量，扫 `k ∈ {3,5,8,10,15,20}`，输出 Hit Rate / Recall / MRR 曲线，
用于在**收益拐点**取 k。

用法：
    python -m evaluation.run_param_sweep
    python -m evaluation.run_param_sweep --ks 3,5,8 --rerank
"""

import argparse
import json
from datetime import datetime

from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging
from evaluation.common import (
    ensure_report_dir,
    eval_config_snapshot,
    load_testset,
    run_system,
)

logger = get_logger(__name__)

DEFAULT_KS = (3, 5, 8, 10, 15, 20)


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def sweep(items, ks: tuple[int, ...], rerank: bool | None) -> list[dict]:
    """对每个 k 计算一次检索指标。"""
    positives = [item for item in items if item.expected_chunk_ids]
    curves: list[dict] = []

    for k in ks:
        hit, reciprocal_ranks, recalls = 0, [], []
        by_type: dict[str, list[dict]] = {}

        for item in positives:
            run = run_system(item, top_k=k, generate_answer=False, rerank=rerank)
            matched = [cid for cid in run.retrieved_ids if cid in item.expected_chunk_ids]
            rank = next(
                (
                    i
                    for i, cid in enumerate(run.retrieved_ids, start=1)
                    if cid in item.expected_chunk_ids
                ),
                0,
            )
            if matched:
                hit += 1
            reciprocal_ranks.append(1.0 / rank if rank else 0.0)
            recalls.append(len(matched) / len(item.expected_chunk_ids))
            by_type.setdefault(item.type, []).append({"hit": bool(matched), "rr": 1.0 / rank if rank else 0.0})

        total = len(positives)
        curves.append(
            {
                "k": k,
                "hit_rate": round(hit / total, 4) if total else 0.0,
                "recall": _mean(recalls),
                "mrr": _mean(reciprocal_ranks),
                "by_type": {
                    kind: {
                        "count": len(rows),
                        "hit_rate": _mean([1.0 if r["hit"] else 0.0 for r in rows]),
                        "mrr": _mean([r["rr"] for r in rows]),
                    }
                    for kind, rows in by_type.items()
                },
            }
        )
        logger.info("k=%d 完成: hit=%.4f recall=%.4f mrr=%.4f", k, curves[-1]["hit_rate"], curves[-1]["recall"], curves[-1]["mrr"])

    return curves


def print_curves(curves: list[dict]) -> None:
    print("\n=== top_k 扫参曲线（正样本）===")
    print(f"  {'k':>4}  {'Hit Rate':>10}  {'Recall':>10}  {'MRR':>10}")
    for row in curves:
        print(f"  {row['k']:>4}  {row['hit_rate']:>10.2%}  {row['recall']:>10.2%}  {row['mrr']:>10.4f}")

    # 收益拐点：Recall 增量首次低于 2 个百分点的 k
    if len(curves) >= 2:
        knee = curves[0]["k"]
        for prev, cur in zip(curves, curves[1:]):
            if cur["recall"] - prev["recall"] < 0.02:
                knee = prev["k"]
                break
            knee = cur["k"]
        print(f"\n  收益拐点（Recall 增量首次 < 2%）：k = {knee}")
        print("  → 该值即为 top_k 的实验依据，可写进配置说明")


def main() -> None:
    parser = argparse.ArgumentParser(description="top_k 扫参")
    parser.add_argument("--ks", default=",".join(str(k) for k in DEFAULT_KS), help="逗号分隔的 k 列表")
    parser.add_argument("--rerank", action="store_true", help="开启重排")
    parser.add_argument("--no-report", action="store_true", help="不写报告文件")
    args = parser.parse_args()

    setup_logging(get_settings().log_level)
    ks = tuple(int(part) for part in args.ks.split(",") if part.strip())

    items = load_testset()
    curves = sweep(items, ks, rerank=True if args.rerank else None)
    print_curves(curves)

    if not args.no_report:
        report = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "ks": list(ks),
            "rerank": bool(args.rerank),
            "config": eval_config_snapshot(),
            "curves": curves,
        }
        path = ensure_report_dir() / f"topk_sweep_{datetime.now():%Y%m%d_%H%M%S}.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n报告已写入: {path}")


if __name__ == "__main__":
    main()
