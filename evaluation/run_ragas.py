"""RAGAS 六指标评估。

对每条评估样本：
    1. 跑被测系统（混合检索 + 本地生成）得到回答与检索上下文；
    2. 组装成 RAGAS 数据集；
    3. 由本地 Ollama 作为 LLM 裁判逐指标打分。

指标与 `开发流程/06-测试与验收方案.md` §6 一致（六个）：
Faithfulness / Answer Relevancy / Context Precision / Context Recall /
Answer Correctness / Context Entity Recall。

**耗时提醒**：每条样本会触发多次 LLM 裁判调用（本地 9B 模型约 1~2s/次），
20 条全量运行可能需要数十分钟。建议先用 `--limit` 小样本验证链路。

用法：
    python -m evaluation.run_ragas --limit 3      # 小样本冒烟
    python -m evaluation.run_ragas                # 全量
    python -m evaluation.run_ragas --judge-model qwen3.5:4b
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
from evaluation.ragas_compat import (
    METRIC_LABELS,
    VERTEXAI_SHIM_INSTALLED,
    build_judge_embeddings,
    build_judge_llm,
    build_metrics,
)

logger = get_logger(__name__)

#: 数据集里非指标列（用于区分分数列）
_NON_SCORE_COLUMNS = {"user_input", "retrieved_contexts", "response", "reference", "reference_contexts"}


def collect_runs(items, *, top_k: int | None, rerank: bool | None) -> list:
    """跑被测系统，收集 (回答, 上下文)。"""
    runs = []
    for index, item in enumerate(items, start=1):
        logger.info("运行被测系统 [%d/%d] %s …", index, len(items), item.id)
        runs.append(run_system(item, top_k=top_k, generate_answer=True, rerank=rerank))
    return runs


def build_dataset(runs):
    """把被测系统输出转成 RAGAS 数据集。"""
    from ragas import EvaluationDataset
    from ragas.dataset_schema import SingleTurnSample

    samples = [
        SingleTurnSample(
            user_input=run.item.question,
            response=run.answer,
            retrieved_contexts=run.contexts,
            reference=run.item.reference_answer,
        )
        for run in runs
    ]
    return EvaluationDataset(samples=samples)


def main() -> None:
    parser = argparse.ArgumentParser(description="RAGAS 六指标评估")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 条（冒烟用）")
    parser.add_argument("--top-k", type=int, default=None, help="检索 Top-K（默认用配置）")
    parser.add_argument("--rerank", action="store_true", help="开启重排")
    parser.add_argument("--judge-model", default=None, help="裁判模型（默认用 LLM_MODEL）")
    parser.add_argument("--judge-thinking", action="store_true", help="允许裁判开启思考模式（默认关闭，关闭快约 33 倍）")
    parser.add_argument("--judge-timeout", type=float, default=600.0, help="单次裁判调用超时（秒）")
    parser.add_argument("--max-workers", type=int, default=2, help="裁判并发数（本地模型建议 1~2）")
    parser.add_argument("--no-report", action="store_true", help="不写报告文件")
    args = parser.parse_args()

    setup_logging(get_settings().log_level)
    if VERTEXAI_SHIM_INSTALLED:
        logger.info("已注入 langchain_community vertexai 兼容 shim（ragas 导入所需）")

    items = load_testset()
    if args.limit:
        items = items[: args.limit]
        logger.warning("仅评估前 %d 条（冒烟模式），结果不代表整体效果", len(items))

    runs = collect_runs(items, top_k=args.top_k, rerank=True if args.rerank else None)

    llm = build_judge_llm(args.judge_model, reasoning=args.judge_thinking)
    embeddings = build_judge_embeddings()
    metrics = build_metrics(llm, embeddings)

    dataset = build_dataset(runs)

    from ragas import evaluate
    from ragas.run_config import RunConfig

    # 本地模型并发能力有限：并发过高会互相拖慢并撞上超时
    run_config = RunConfig(timeout=args.judge_timeout, max_workers=args.max_workers)

    logger.info(
        "开始 RAGAS 评估（%d 条 × %d 指标，timeout=%.0fs，max_workers=%d）…",
        len(items),
        len(metrics),
        args.judge_timeout,
        args.max_workers,
    )
    result = evaluate(
        dataset=dataset,
        metrics=metrics,
        llm=llm,
        embeddings=embeddings,
        run_config=run_config,
        show_progress=True,
    )

    frame = result.to_pandas()
    score_columns = [c for c in frame.columns if c not in _NON_SCORE_COLUMNS]
    scores = {
        column: round(float(frame[column].mean(skipna=True)), 4)
        for column in score_columns
        if frame[column].dtype.kind in "fi"
    }

    print("\n=== RAGAS 六指标 ===")
    for name, value in scores.items():
        label = METRIC_LABELS.get(name, "")
        print(f"  {name:<24s} {value:.4f}   {label}")

    print("\n  逐条得分：")
    for index, row in frame.iterrows():
        item = runs[index].item
        detail = " ".join(f"{c}={row[c]:.2f}" for c in scores if row.get(c) is not None)
        print(f"    {item.id} [{item.type}] {detail}")

    if not args.no_report:
        report = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "samples": len(items),
            "limited": bool(args.limit),
            "top_k": args.top_k or get_settings().top_k,
            "rerank": bool(args.rerank),
            "judge_model": args.judge_model or get_settings().llm_model,
            "config": eval_config_snapshot(),
            "metrics": scores,
            "per_sample": [
                {
                    "id": runs[i].item.id,
                    "type": runs[i].item.type,
                    "question": runs[i].item.question,
                    "retrieved_ids": runs[i].retrieved_ids,
                    "mode": runs[i].mode,
                    **{c: (None if frame[c].isna().iloc[i] else round(float(frame[c].iloc[i]), 4)) for c in scores},
                }
                for i in range(len(runs))
            ],
        }
        path = ensure_report_dir() / f"ragas_{datetime.now():%Y%m%d_%H%M%S}.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n报告已写入: {path}")


if __name__ == "__main__":
    main()
