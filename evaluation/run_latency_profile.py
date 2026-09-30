"""延迟剖析：分阶段耗时 + 模型/输出长度对比。

回答「一次问答到底慢在哪」，并给出可落地的优化依据（对应要点文档 §3.7「延迟预算」）。

两个阶段：

| 阶段 | 测什么 |
| --- | --- |
| 检索阶段 | embedding / 稠密检索 / 稀疏检索 / 混合检索整体 |
| 生成阶段 | 首 token 延迟（TTFT）、总耗时、输出 token 数、生成速率（tok/s） |

对比维度：

- **模型**（`--models`）：如 `qwen3.5:9b` vs `qwen3.5:4b`
- **输出长度上限**（`--num-predicts`）：0 表示不限制；限制输出是本地生成最直接的一刀
- **模型冷/热**：先做一次预热调用，观察 `load_duration`（冷加载代价）

用法：
    python -m evaluation.run_latency_profile --queries 3
    python -m evaluation.run_latency_profile --models qwen3.5:9b,qwen3.5:4b --num-predicts 0,256
"""

import argparse
import json
import statistics
import time
from datetime import datetime

from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging
from app.generation.ollama import generate_detailed
from app.generation.prompts import SYSTEM_PROMPT, USER_PROMPT_TEMPLATE, build_context
from app.retrieval.dense import embed, search_dense
from app.retrieval.hybrid import hybrid_search
from app.retrieval.sparse import search_sparse
from evaluation.common import ensure_report_dir, load_testset

logger = get_logger(__name__)

#: 预热用的极短提示（只为把模型加载进内存）
WARMUP_SYSTEM = "你是一个助手。"
WARMUP_USER = "回复「好」一个字。"


def _percentile(values: list[float], ratio: float) -> float:
    """简单分位数（样本量小时足够）。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(ratio * (len(ordered) - 1))))
    return round(ordered[index], 3)


def _summary(values: list[float]) -> dict:
    return {
        "count": len(values),
        "mean": round(statistics.mean(values), 3) if values else 0.0,
        "p50": _percentile(values, 0.5),
        "p95": _percentile(values, 0.95),
        "min": round(min(values), 3) if values else 0.0,
        "max": round(max(values), 3) if values else 0.0,
    }


def profile_retrieval(queries: list[str], top_k: int, top_n: int) -> dict:
    """检索阶段分阶段计时。

    先做一次 embedding 预热：模型冷加载可达数秒，若不预热，
    第一条 query 的 `embed`（冷）会比 `search_dense` 内部的 embed（热）慢得多，
    导致「稠密检索耗时」被算成负数。
    """
    warm_start = time.perf_counter()
    embed(["预热"])
    logger.info("embedding 预热完成: %.0fms", (time.perf_counter() - warm_start) * 1000)

    rows = []
    for query in queries:
        start = time.perf_counter()
        embed([query])
        embed_ms = (time.perf_counter() - start) * 1000

        start = time.perf_counter()
        search_dense(query, top_n)
        dense_ms = (time.perf_counter() - start) * 1000

        start = time.perf_counter()
        search_sparse(query, top_n)
        sparse_ms = (time.perf_counter() - start) * 1000

        start = time.perf_counter()
        results, mode, _debug = hybrid_search(query, top_k=top_k)
        hybrid_ms = (time.perf_counter() - start) * 1000

        rows.append(
            {
                "query": query,
                "embed_ms": round(embed_ms, 1),
                "dense_ms": round(dense_ms, 1),
                "dense_search_ms": round(dense_ms - embed_ms, 1),
                "sparse_ms": round(sparse_ms, 1),
                "hybrid_ms": round(hybrid_ms, 1),
                "mode": mode.value,
                "hits": len(results),
            }
        )

    return {
        "rows": rows,
        "summary": {
            key: _summary([row[key] for row in rows])
            for key in ("embed_ms", "dense_ms", "dense_search_ms", "sparse_ms", "hybrid_ms")
        },
    }


def profile_generation(queries: list[str], model: str, num_predict: int, top_k: int) -> dict:
    """生成阶段计时（含预热与冷加载观测）。"""
    # 预热：把模型加载进内存，并记录冷加载耗时
    warm = generate_detailed(WARMUP_SYSTEM, WARMUP_USER, model=model, num_predict=8)
    logger.info("[%s] 预热完成: 总耗时=%.1fs 加载=%.1fs", model, warm.total_seconds, warm.load_seconds)

    rows = []
    for query in queries:
        chunks, _mode, _debug = hybrid_search(query, top_k=top_k)
        context = build_context(chunks)
        user_prompt = USER_PROMPT_TEMPLATE.format(context=context, query=query)

        metrics = generate_detailed(
            SYSTEM_PROMPT, user_prompt, model=model, num_predict=num_predict or None
        )
        row = metrics.to_dict()
        row["query"] = query
        rows.append(row)
        logger.info(
            "[%s num_predict=%d] TTFT=%.1fs 总=%.1fs 输出=%d tok",
            model,
            num_predict,
            metrics.ttft_seconds or 0.0,
            metrics.total_seconds,
            metrics.output_tokens,
        )

    return {
        "model": model,
        "num_predict": num_predict,
        "warmup": {"total_seconds": warm.total_seconds, "load_seconds": warm.load_seconds},
        "rows": rows,
        "summary": {
            "ttft_seconds": _summary([r["ttft_seconds"] or 0.0 for r in rows]),
            "total_seconds": _summary([r["total_seconds"] for r in rows]),
            "output_tokens": _summary([float(r["output_tokens"]) for r in rows]),
            "prompt_tokens": _summary([float(r["prompt_tokens"]) for r in rows]),
            "tokens_per_second": _summary([r["output_tokens_per_second"] for r in rows]),
        },
    }


def print_retrieval_report(result: dict) -> None:
    print("\n=== 检索阶段（毫秒）===")
    print(f"  {'query':<34} {'embed':>7} {'稠密':>7} {'稀疏':>7} {'混合':>7} {'mode':>12}")
    for row in result["rows"]:
        query = row["query"][:32]
        print(
            f"  {query:<34} {row['embed_ms']:>7.1f} {row['dense_search_ms']:>7.1f} "
            f"{row['sparse_ms']:>7.1f} {row['hybrid_ms']:>7.1f} {row['mode']:>12}"
        )
    summary = result["summary"]
    print("\n  汇总：")
    for key, label in (
        ("embed_ms", "embedding"),
        ("dense_search_ms", "稠密检索(Milvus)"),
        ("sparse_ms", "稀疏检索(BM25)"),
        ("hybrid_ms", "混合检索整体"),
    ):
        stats = summary[key]
        print(f"    {label:<18} mean={stats['mean']:>7.1f}ms  p95={stats['p95']:>7.1f}ms")


def print_generation_report(results: list[dict]) -> None:
    print("\n=== 生成阶段 ===")
    header = f"  {'model':<16} {'num_predict':>11} {'TTFT(s)':>9} {'总耗时(s)':>10} {'输出tok':>8} {'tok/s':>7}"
    print(header)
    for result in results:
        summary = result["summary"]
        print(
            f"  {result['model']:<16} {result['num_predict']:>11} "
            f"{summary['ttft_seconds']['mean']:>9.1f} {summary['total_seconds']['mean']:>10.1f} "
            f"{summary['output_tokens']['mean']:>8.0f} {summary['tokens_per_second']['mean']:>7.1f}"
        )

    print("\n  冷加载观测（预热调用的 load_duration）：")
    for result in results:
        print(f"    {result['model']:<16} load={result['warmup']['load_seconds']:.1f}s")

    print("\n  延迟构成（均值）：")
    for result in results:
        summary = result["summary"]
        ttft = summary["ttft_seconds"]["mean"]
        total = summary["total_seconds"]["mean"]
        print(
            f"    {result['model']:<16} 首token={ttft:.1f}s（prefill） + "
            f"后续生成={max(total - ttft, 0):.1f}s = {total:.1f}s"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="延迟剖析")
    parser.add_argument("--queries", type=int, default=3, help="取样问题数（取自评估集正样本）")
    parser.add_argument("--models", default=None, help="逗号分隔的模型列表（默认当前配置）")
    parser.add_argument("--num-predicts", default="0", help="逗号分隔的输出上限（0=不限）")
    parser.add_argument("--top-k", type=int, default=None, help="检索 Top-K（默认用配置）")
    parser.add_argument("--top-n", type=int, default=20, help="单通道召回条数")
    parser.add_argument("--skip-retrieval", action="store_true", help="跳过检索阶段")
    parser.add_argument("--skip-generation", action="store_true", help="跳过生成阶段")
    parser.add_argument("--no-report", action="store_true", help="不写报告文件")
    args = parser.parse_args()

    setup_logging(get_settings().log_level)
    settings = get_settings()
    top_k = args.top_k or settings.top_k

    items = [item for item in load_testset() if item.expected_chunk_ids][: args.queries]
    queries = [item.question for item in items]
    print(f"取样 {len(queries)} 条问题（Top-K={top_k}）")

    report: dict = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "queries": queries,
        "top_k": top_k,
    }

    if not args.skip_retrieval:
        retrieval = profile_retrieval(queries, top_k=top_k, top_n=args.top_n)
        print_retrieval_report(retrieval)
        report["retrieval"] = retrieval

    if not args.skip_generation:
        models = (
            [m.strip() for m in args.models.split(",") if m.strip()]
            if args.models
            else [settings.llm_model]
        )
        num_predicts = [int(p) for p in args.num_predicts.split(",") if p.strip()]

        results = []
        for model in models:
            for num_predict in num_predicts:
                results.append(
                    profile_generation(queries, model=model, num_predict=num_predict, top_k=top_k)
                )
        print_generation_report(results)
        report["generation"] = results

    if not args.no_report:
        path = ensure_report_dir() / f"latency_{datetime.now():%Y%m%d_%H%M%S}.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n报告已写入: {path}")


if __name__ == "__main__":
    main()
