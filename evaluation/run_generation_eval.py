"""生成层评估：拒答率 + 引用回链正确率（不需要 LLM 裁判）。

对应 `开发流程/06-测试与验收方案.md` §5.5 与要点文档 §3.5 / §3.6。

**为什么必须单独跑一层**：检索层只回答「该召回的有没有召回」，
生成层才回答「给了正确资料后答得对不对、知不知道什么时候该闭嘴」。
两层混在一起测，指标掉了无法归因。

指标：

| 指标 | 含义 | 目标 |
| --- | --- | --- |
| 负样本拒答率 | 语料里没有答案时，是否承认「无法确定」 | ≥ 0.90 |
| 引用回链正确率 | 引用有效 + 回答中的版本号/错误码都能在被引片段中找到 | ≥ 0.85 |
| 有引用比例 | 回答中带了 `[来源: chunk_id]` 的比例 | — |
| 回答率 | 正样本中给出实质回答（非拒答）的比例 | — |

用法：
    python -m evaluation.run_generation_eval
    python -m evaluation.run_generation_eval --limit 5      # 冒烟
"""

import argparse
import json
from datetime import datetime

from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging
from app.generation.citations import is_refusal, verify_citations
from evaluation.common import (
    eval_config_snapshot,
    ensure_report_dir,
    load_testset,
    run_system,
)

logger = get_logger(__name__)


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def evaluate(items, *, top_k: int | None, rerank: bool | None) -> dict:
    """分别统计正样本与负样本的生成层表现。"""
    positives = [item for item in items if item.expected_chunk_ids]
    negatives = [item for item in items if not item.expected_chunk_ids]

    positive_rows = []
    for index, item in enumerate(positives, start=1):
        logger.info("[正样本 %d/%d] %s …", index, len(positives), item.id)
        run = run_system(item, top_k=top_k, generate_answer=True, rerank=rerank)
        report = verify_citations(run.answer, run.retrieved)
        positive_rows.append(
            {
                "id": item.id,
                "type": item.type,
                "question": item.question,
                "answer": run.answer,
                "is_refusal": is_refusal(run.answer),
                "citations": report.citations,
                "invalid_citations": report.invalid_citations,
                "ungrounded_entities": report.ungrounded_entities,
                "citation_ok": report.ok,
                "retrieved_ids": run.retrieved_ids,
            }
        )

    negative_rows = []
    for index, item in enumerate(negatives, start=1):
        logger.info("[负样本 %d/%d] %s …", index, len(negatives), item.id)
        run = run_system(item, top_k=top_k, generate_answer=True, rerank=rerank)
        negative_rows.append(
            {
                "id": item.id,
                "question": item.question,
                "answer": run.answer,
                "refused": is_refusal(run.answer),
                "retrieved_ids": run.retrieved_ids,
            }
        )

    return {
        "positive": {
            "count": len(positive_rows),
            "citation_ok_rate": _mean([1.0 if r["citation_ok"] else 0.0 for r in positive_rows]),
            "has_citation_rate": _mean([1.0 if r["citations"] else 0.0 for r in positive_rows]),
            "answered_rate": _mean([0.0 if r["is_refusal"] else 1.0 for r in positive_rows]),
            "rows": positive_rows,
        },
        "negative": {
            "count": len(negative_rows),
            "refusal_rate": _mean([1.0 if r["refused"] else 0.0 for r in negative_rows]),
            "rows": negative_rows,
        },
    }


def print_report(result: dict) -> None:
    positive, negative = result["positive"], result["negative"]

    print(f"\n=== 生成层评估（正样本 {positive['count']} 条）===")
    print(f"  引用回链正确率: {positive['citation_ok_rate']:.2%}   （目标 ≥ 85%）")
    print(f"  有引用比例    : {positive['has_citation_rate']:.2%}")
    print(f"  回答率        : {positive['answered_rate']:.2%}")

    bad = [r for r in positive["rows"] if not r["citation_ok"]]
    if bad:
        print(f"\n  引用校验未通过的样本（{len(bad)} 条）：")
        for row in bad[:10]:
            print(f"    {row['id']} 引用={row['citations']} 无效引用={row['invalid_citations']} "
                  f"未落地实体={row['ungrounded_entities']}")

    print(f"\n=== 负样本拒答（{negative['count']} 条，目标 ≥ 90%）===")
    print(f"  拒答率: {negative['refusal_rate']:.2%}")

    missed = [r for r in negative["rows"] if not r["refused"]]
    if missed:
        print(f"\n  未拒答（可能编造）的样本（{len(missed)} 条）：")
        for row in missed:
            answer = (row["answer"] or "").replace("\n", " ")[:110]
            print(f"    {row['id']} {row['question']}")
            print(f"       回答: {answer}…")
    else:
        print("  全部正确拒答 ✅")


def main() -> None:
    parser = argparse.ArgumentParser(description="生成层评估（拒答率 + 引用回链）")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 条（冒烟）")
    parser.add_argument("--positives", type=int, default=None, help="只跑前 N 条正样本（冒烟）")
    parser.add_argument("--negatives", type=int, default=None, help="只跑前 N 条负样本（冒烟）")
    parser.add_argument("--top-k", type=int, default=None, help="检索 Top-K（默认用配置）")
    parser.add_argument("--rerank", action="store_true", help="开启重排")
    parser.add_argument("--no-report", action="store_true", help="不写报告文件")
    args = parser.parse_args()

    setup_logging(get_settings().log_level)
    items = load_testset()
    if args.limit:
        items = items[: args.limit]
        logger.warning("仅评估前 %d 条（冒烟模式）", len(items))
    elif args.positives is not None or args.negatives is not None:
        positives = [i for i in items if i.expected_chunk_ids]
        negatives = [i for i in items if not i.expected_chunk_ids]
        if args.positives is not None:
            positives = positives[: args.positives]
        if args.negatives is not None:
            negatives = negatives[: args.negatives]
        items = positives + negatives
        logger.warning(
            "冒烟模式：正样本 %d 条 + 负样本 %d 条，结果不代表整体", len(positives), len(negatives)
        )

    result = evaluate(items, top_k=args.top_k, rerank=True if args.rerank else None)
    print_report(result)

    if not args.no_report:
        report = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "samples": len(items),
            "top_k": args.top_k or get_settings().top_k,
            "rerank": bool(args.rerank),
            "config": eval_config_snapshot(),
            **result,
        }
        path = ensure_report_dir() / f"generation_{datetime.now():%Y%m%d_%H%M%S}.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n报告已写入: {path}")


if __name__ == "__main__":
    main()
