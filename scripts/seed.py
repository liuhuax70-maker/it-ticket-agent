"""导入语料并建立索引。

调用链路：ingestion /ingest -> (解析 + 切分 + Postgres) -> indexing -> Milvus/OpenSearch

用法：
    python scripts/seed.py                      # 导入 data/corpus
    python scripts/seed.py --path data/corpus --reindex
    python scripts/seed.py --check              # 只打印索引规模
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

INGESTION_URL = "http://localhost:8004"
RETRIEVAL_URL = "http://localhost:8002"


async def seed(path: str, reindex: bool) -> int:
    payload = {"path": path, "reindex": reindex}
    async with httpx.AsyncClient(timeout=600.0) as client:
        resp = await client.post(f"{INGESTION_URL}/ingest", json=payload)
        if resp.status_code >= 400:
            print(f"[seed] 接入失败 HTTP {resp.status_code}: {resp.text[:500]}")
            return 1
        result = resp.json()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(
        f"\n[seed] 文档 {result['documents']} 篇，分块 {result['chunk_count']} 个，"
        f"已入库 {result['indexed']} 条"
    )
    return 0


async def check() -> int:
    async with httpx.AsyncClient(timeout=30.0) as client:
        health = await client.get(f"{RETRIEVAL_URL}/health")
        print(json.dumps(health.json(), ensure_ascii=False, indent=2))
    return 0 if health.status_code == 200 else 1


async def main() -> int:
    parser = argparse.ArgumentParser(description="导入语料并建立索引")
    parser.add_argument("--path", default="./data/corpus", help="文件或目录路径")
    parser.add_argument("--reindex", action="store_true", help="先清理该文档的旧索引再写入")
    parser.add_argument("--check", action="store_true", help="只查看索引规模")
    args = parser.parse_args()

    if args.check:
        return await check()
    return await seed(args.path, args.reindex)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
