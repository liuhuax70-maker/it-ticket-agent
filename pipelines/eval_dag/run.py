"""评测跑批任务。

调用 eval 服务的 /eval/run，适合放进 CI（每次改动提示词/切分参数后跑一次回归）
或 nightly 定时任务。

用法：
    python -m pipelines.eval_dag.run                  # 全量（L1 + L2 RAGAS）
    python -m pipelines.eval_dag.run --no-ragas       # 只跑 L1，秒级到分钟级，适合迭代
    python -m pipelines.eval_dag.run --limit 20       # 只跑前 20 条
    python -m pipelines.eval_dag.run --dataset configs/eval/golden.jsonl
    python -m pipelines.eval_dag.run --preflight      # 只做前置检查，不调用模型
    python -m pipelines.eval_dag.run --rescore        # 不重新采集，用上次结果重打分

退出码：0 通过；1 请求/评测失败；2 出现越权泄露（绝对不变量，不设阈值）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# 默认指向本机；CI / 定时任务用环境变量指向预发或生产
EVAL_URL = os.getenv("RAG_EVAL_URL", "http://localhost:8006")


def _print_summary(summary: dict) -> None:
    def pct(value: object) -> str:
        return "—" if value is None else f"{float(value) * 100:.1f}%"

    print("\n=== L1 确定性指标 ===")
    print(
        f"  样本            {summary.get('count')}（正 {summary.get('positive')} / 负 {summary.get('negative')}）"
    )
    print(f"  hit@k           {pct(summary.get('hit_at_k'))}   区间 {summary.get('hit_at_k_ci95')}")
    print(f"  MRR             {summary.get('mrr')}")
    print(f"  片段召回        {pct(summary.get('snippet_recall'))}")
    print(f"  引用覆盖率      {pct(summary.get('citation_coverage'))}")
    print(f"  漏答率          {pct(summary.get('false_refusal_rate'))}")
    print(f"  误答率          {pct(summary.get('false_answer_rate'))}")
    print(f"  越权泄露        {summary.get('leak_count')} 条")
    print(
        f"  延迟 P50/P95     {summary.get('latency_ms_p50')} / {summary.get('latency_ms_p95')} ms"
    )
    print(f"  鉴权模式        {summary.get('authz_mode')}")

    ragas = summary.get("ragas") or {}
    if ragas:
        print("\n=== L2 RAGAS ===")
        for key, value in ragas.items():
            print(f"  {key:<24} {value}")
        if summary.get("judge_model"):
            print(f"  裁判模型          {summary['judge_model']}")


async def _post(client: httpx.AsyncClient, path: str, payload: dict) -> tuple[int, dict]:
    try:
        resp = await client.post(f"{EVAL_URL}{path}", json=payload)
    except Exception as exc:  # noqa: BLE001
        print(f"[eval] 请求失败（eval 服务是否已启动？）: {exc}")
        return 1, {}
    if resp.status_code >= 400:
        print(f"[eval] HTTP {resp.status_code}: {resp.text[:800]}")
        return 1, {}
    return 0, resp.json()


async def run(args: argparse.Namespace) -> int:
    payload: dict = {"dataset_path": args.dataset, "limit": args.limit, "write_report": True}
    async with httpx.AsyncClient(timeout=7200.0) as client:
        if args.preflight:
            code, body = await _post(
                client, "/eval/preflight", {"dataset_path": args.dataset, "limit": args.limit}
            )
            if code:
                return code
            print(json.dumps(body, ensure_ascii=False, indent=2))
            if body.get("missing_sources"):
                print("\n[eval] 语料未就绪，先执行：python scripts/prepare_corpus.py")
                return 1
            return 0

        if args.rescore:
            # 复用上次落盘的采集结果：调裁判/指标/超时策略时不必重打一遍真实链路
            code, body = await _post(
                client, "/eval/score", {"report_path": args.report, "write_report": True}
            )
            if code:
                return code
            print(f"[eval] 已用 {body.get('source')} 的采集结果重打分")
        else:
            payload["run_ragas"] = None if args.ragas else False
            code, body = await _post(client, "/eval/run", payload)
            if code:
                return code

    summary = body.get("summary", {}) or {}
    preflight = body.get("preflight", {}) or {}
    if preflight.get("authz_mode") == "fixed-identity":
        print(
            "\n[eval] 警告：取不到 Keycloak 令牌，权限类样本以固定身份执行，"
            "结果不能用于判断越权行为"
        )
    _print_summary(summary)
    if body.get("report_path"):
        print(f"\n[eval] 报告：{body['report_path']}（Markdown 版同目录 *_latest.md）")

    if summary.get("confidence") == "insufficient_samples":
        print(f"[eval] 注意：样本数 {summary.get('count')} 少于 30，指标仅有参考价值")

    if (summary.get("leak_count") or 0) > 0:
        print("[eval] 失败：检测到越权泄露，这是绝对不变量，请先修权限过滤")
        return 2
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="RAG 评测跑批（L1 确定性 + L2 RAGAS）")
    parser.add_argument("--dataset", default=None, help="评测集路径（默认用服务配置）")
    parser.add_argument("--limit", type=int, default=None, help="样本数上限")
    parser.add_argument(
        "--no-ragas", dest="ragas", action="store_false", help="只跑 L1，跳过 RAGAS"
    )
    parser.add_argument("--preflight", action="store_true", help="只做前置检查")
    parser.add_argument(
        "--rescore", action="store_true", help="不重新采集，用上次落盘的采集结果重打分"
    )
    parser.add_argument("--report", default=None, help="--rescore 时指定采集结果 JSON")
    parser.set_defaults(ragas=True)
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
