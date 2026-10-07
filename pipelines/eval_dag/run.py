"""评测跑批任务。

调用 eval 服务的 /eval/run，适合放进 CI（每次改动提示词/切分参数后跑一次回归）
或 nightly 定时任务。

用法：
    python -m pipelines.eval_dag.run                     # 全量
    python -m pipelines.eval_dag.run --limit 20          # 只跑前 20 条
    python -m pipelines.eval_dag.run --dataset eval_data/golden.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

EVAL_URL = "http://localhost:8006"


async def run(dataset: str | None, limit: int | None) -> int:
    payload = {"dataset_path": dataset, "limit": limit, "write_report": True}
    async with httpx.AsyncClient(timeout=3600.0) as client:
        try:
            resp = await client.post(f"{EVAL_URL}/eval/run", json=payload)
        except Exception as exc:  # noqa: BLE001
            print(f"[eval] 请求失败（eval 服务是否已启动？）: {exc}")
            return 1

    if resp.status_code >= 400:
        print(f"[eval] 评测失败 HTTP {resp.status_code}: {resp.text[:800]}")
        return 1

    body = resp.json()
    print(json.dumps(body, ensure_ascii=False, indent=2))
    summary = body.get("summary", {})
    if summary.get("confidence") == "insufficient_samples":
        print(f"\n[eval] 注意：样本数 {summary.get('count')} 少于 30，指标仅有参考价值")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="RAGAS 评测跑批")
    parser.add_argument("--dataset", default=None)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    return asyncio.run(run(args.dataset, args.limit))


if __name__ == "__main__":
    raise SystemExit(main())
