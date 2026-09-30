"""相关性门槛校准：给「这个工单有没有可用资料」找一个可解释的分数阈值。

## 为什么需要它

当前检索层**没有相关性门槛**：任何问题都会返回 Top-K 片段，
负样本（知识库范围外的问题）100% 拿到 8 条无关片段，
「该不该拒答」完全交给生成层兜底。

## 为什么做成「查询级」而不是「片段级」

片段级标注有天然歧义：**不在 `expected_chunk_ids` 里 ≠ 不相关**。
一个问题的答案可能同时出现在多个片段，把非 gold 片段标成「不相关」会系统性污染阈值。

而真实决策是查询级的：**这个工单到底有没有可用资料？**
所以：

- 正样本（164 条）= 有可用资料（label 1）
- 负样本（36 条）= 没有可用资料（label 0）
- 特征 = 该次检索的**最高分**（max_dense / max_sparse / top1_rrf）

## 两种误判的代价不对称

| 误判 | 含义 | 代价 |
| --- | --- | --- |
| 漏判（false reject） | 真问题被判为「无资料」→ 拒答 | **高**：用户得不到答案，体验直接受损 |
| 误收（false accept） | 无关问题仍拿到片段 | 低：生成层还有一道拒答闸 |

因此阈值应**偏向保召回**，宁可误收也不能漏判。报告里会同时给出两者数量。

## 防过拟合

阈值在 200 条上直接选必然过拟合，故做 **5 折交叉验证**：
阈值只在训练折上选，在测试折上评估，取平均作为可信指标。

用法：
    python -m evaluation.calibrate_threshold
    python -m evaluation.calibrate_threshold --folds 5
"""

import argparse
import json
import random
import statistics
from datetime import datetime

from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging
from app.retrieval.hybrid import hybrid_search
from evaluation.common import ensure_report_dir, load_testset

logger = get_logger(__name__)

FEATURES = ("max_dense", "max_sparse", "top1_rrf")


def collect_features(items: list) -> list[dict]:
    """对每条样本跑一次检索，取各通道最高分作为特征。"""
    rows: list[dict] = []
    for item in items:
        chunks, mode, _debug = hybrid_search(item.question)

        dense = [c.dense_score for c in chunks if c.dense_score is not None]
        sparse = [c.sparse_score for c in chunks if c.sparse_score is not None]

        rows.append(
            {
                "id": item.id,
                "type": item.type,
                "question": item.question,
                "label": 0 if item.type == "negative" else 1,
                "max_dense": max(dense) if dense else 0.0,
                "max_sparse": max(sparse) if sparse else 0.0,
                "top1_rrf": chunks[0].rrf_score if chunks and chunks[0].rrf_score is not None else 0.0,
                "n_chunks": len(chunks),
                "mode": mode.value,
            }
        )
    return rows


def _scores(rows: list[dict], key: str, threshold: float) -> dict:
    """给定阈值，计算混淆矩阵与指标（label=1 表示有可用资料）。"""
    tp = fp = fn = tn = 0
    for row in rows:
        predicted = row[key] >= threshold
        actual = row["label"] == 1
        if predicted and actual:
            tp += 1
        elif predicted and not actual:
            fp += 1
        elif not predicted and actual:
            fn += 1
        else:
            tn += 1

    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "threshold": round(threshold, 4),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
    }


def sweep(rows: list[dict], key: str) -> dict:
    """在给定数据上扫出 F1 最优阈值。"""
    values = sorted({row[key] for row in rows})
    best: dict | None = None
    for threshold in values:
        scores = _scores(rows, key, threshold)
        if best is None or scores["f1"] > best["f1"]:
            best = scores
    return best or {}


def cross_validate(rows: list[dict], key: str, folds: int = 5, seed: int = 42) -> dict:
    """k 折：阈值只在训练折上选，在测试折上评估。"""
    shuffled = list(rows)
    random.Random(seed).shuffle(shuffled)

    fold_metrics: list[dict] = []
    thresholds: list[float] = []

    for fold in range(folds):
        test = shuffled[fold::folds]
        train = [row for i, row in enumerate(shuffled) if i % folds != fold]

        best = sweep(train, key)
        thresholds.append(best["threshold"])
        fold_metrics.append(_scores(test, key, best["threshold"]))

    return {
        "folds": folds,
        "threshold_median": round(statistics.median(thresholds), 4),
        "threshold_range": [round(min(thresholds), 4), round(max(thresholds), 4)],
        "precision": round(statistics.mean(m["precision"] for m in fold_metrics), 4),
        "recall": round(statistics.mean(m["recall"] for m in fold_metrics), 4),
        "f1": round(statistics.mean(m["f1"] for m in fold_metrics), 4),
        "mean_false_reject": round(statistics.mean(m["fn"] for m in fold_metrics), 2),
        "mean_false_accept": round(statistics.mean(m["fp"] for m in fold_metrics), 2),
    }


def distribution(rows: list[dict], key: str) -> dict:
    """正/负样本在该特征上的分布，用于判断可分性。"""
    def stats(values: list[float]) -> dict:
        ordered = sorted(values)
        return {
            "min": round(ordered[0], 4),
            "p50": round(ordered[len(ordered) // 2], 4),
            "max": round(ordered[-1], 4),
            "mean": round(statistics.mean(values), 4),
        }

    positive = [row[key] for row in rows if row["label"] == 1]
    negative = [row[key] for row in rows if row["label"] == 0]
    return {"positive": stats(positive), "negative": stats(negative)}


def print_report(rows: list[dict], results: dict, folds: int) -> None:
    print(f"\n样本：{len(rows)} 条（正样本 {sum(r['label'] for r in rows)} / 负样本 {sum(1 - r['label'] for r in rows)}）")

    for key in FEATURES:
        dist = results[key]["distribution"]
        print(f"\n=== 特征 {key} ===")
        print(
            f"  正样本: min={dist['positive']['min']:.3f} p50={dist['positive']['p50']:.3f} "
            f"max={dist['positive']['max']:.3f} 均值={dist['positive']['mean']:.3f}"
        )
        print(
            f"  负样本: min={dist['negative']['min']:.3f} p50={dist['negative']['p50']:.3f} "
            f"max={dist['negative']['max']:.3f} 均值={dist['negative']['mean']:.3f}"
        )

        best = results[key]["best"]
        print(
            f"  全量最优: 阈值={best['threshold']:.3f} "
            f"precision={best['precision']:.3f} recall={best['recall']:.3f} F1={best['f1']:.3f}"
        )
        print(
            f"             漏判(真问题被拒)={best['fn']}  误收(无关问题放行)={best['fp']}"
        )

        cv = results[key]["cross_validation"]
        print(
            f"  {folds}折CV : 阈值中位数={cv['threshold_median']:.3f} "
            f"precision={cv['precision']:.3f} recall={cv['recall']:.3f} F1={cv['f1']:.3f}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="相关性门槛校准")
    parser.add_argument("--folds", type=int, default=5, help="交叉验证折数")
    parser.add_argument("--no-report", action="store_true", help="不写报告文件")
    args = parser.parse_args()

    setup_logging(get_settings().log_level)

    items = load_testset()
    print(f"加载评估集 {len(items)} 条，开始采集检索分数...")
    rows = collect_features(items)

    results = {}
    for key in FEATURES:
        results[key] = {
            "distribution": distribution(rows, key),
            "best": sweep(rows, key),
            "cross_validation": cross_validate(rows, key, folds=args.folds),
        }

    print_report(rows, results, args.folds)

    if not args.no_report:
        path = ensure_report_dir() / f"threshold_{datetime.now():%Y%m%d_%H%M%S}.json"
        path.write_text(
            json.dumps(
                {"generated_at": datetime.now().isoformat(timespec="seconds"), "features": results, "rows": rows},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n报告已写入: {path}")


if __name__ == "__main__":
    main()
