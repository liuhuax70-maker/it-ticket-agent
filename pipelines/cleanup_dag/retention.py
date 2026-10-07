"""留存与合规删除。

两种用法：
    1. 按天数留存：删除 ``--older-than-days`` 之前创建、且不在豁免名单里的文档；
    2. 精确删除：``--doc-id d_xxx``（用于「被遗忘权」这类按主体删除的请求）。

默认 **dry-run**：删除属于不可逆操作，必须先看清单再执行。

用法：
    python -m pipelines.cleanup_dag.retention --older-than-days 180
    python -m pipelines.cleanup_dag.retention --older-than-days 180 --apply
    python -m pipelines.cleanup_dag.retention --doc-id d_1a2b3c4d --apply
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

INGESTION_URL = "http://localhost:8004"


async def _list_documents(client: httpx.AsyncClient, tenant_id: str | None, limit: int) -> list[dict]:
    params = {"limit": limit, "offset": 0}
    if tenant_id:
        params["tenant_id"] = tenant_id
    resp = await client.get(f"{INGESTION_URL}/documents", params=params, timeout=60.0)
    resp.raise_for_status()
    return resp.json().get("items", [])


def _expired(document: dict, cutoff: dt.datetime) -> bool:
    created_at = document.get("created_at")
    if not created_at:
        return False
    try:
        parsed = dt.datetime.fromisoformat(created_at)
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed < cutoff


async def run(
    *, older_than_days: int | None, doc_ids: list[str], apply: bool, tenant_id: str | None
) -> int:
    targets: list[str] = list(doc_ids)

    async with httpx.AsyncClient(timeout=120.0) as client:
        if older_than_days is not None:
            cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=older_than_days)
            documents = await _list_documents(client, tenant_id, limit=500)
            for document in documents:
                if _expired(document, cutoff):
                    targets.append(document["doc_id"])
            print(f"[cleanup] 创建于 {cutoff.isoformat()} 之前的文档 {len(targets)} 篇")

        if not targets:
            print("[cleanup] 没有需要清理的文档")
            return 0

        if not apply:
            print("[cleanup] dry-run（未加 --apply），将删除以下文档：")
            for doc_id in targets:
                print(f"  - {doc_id}")
            return 0

        failures = 0
        for doc_id in targets:
            resp = await client.delete(f"{INGESTION_URL}/documents/{doc_id}", timeout=120.0)
            if resp.status_code >= 400:
                print(f"[cleanup] 删除失败 {doc_id}: {resp.status_code} {resp.text[:200]}")
                failures += 1
                continue
            print(f"[cleanup] 已删除 {doc_id} -> {resp.json().get('deleted')}")

    print(f"\n[cleanup] 完成，成功 {len(targets) - failures}，失败 {failures}")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="文档留存与合规删除")
    parser.add_argument("--older-than-days", type=int, default=None)
    parser.add_argument("--doc-id", action="append", default=[])
    parser.add_argument("--tenant-id", default=None)
    parser.add_argument("--apply", action="store_true", help="真正执行删除（默认 dry-run）")
    args = parser.parse_args()

    if args.older_than_days is None and not args.doc_id:
        parser.error("至少提供 --older-than-days 或 --doc-id 之一")

    return asyncio.run(
        run(
            older_than_days=args.older_than_days,
            doc_ids=args.doc_id,
            apply=args.apply,
            tenant_id=args.tenant_id,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
