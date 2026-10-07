"""全量 / 增量同步本地语料目录。

增量判据：doc_id 是否存在 + content_hash 是否变化。
两者都相同才跳过——这样既不会漏同步（改了内容），也不会重复建索引（没改内容）。

用法：
    python -m pipelines.ingestion_dag.sync --path data/corpus
    python -m pipelines.ingestion_dag.sync --path data/corpus --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from packages.common.ids import content_hash, stable_doc_id  # noqa: E402

INGESTION_URL = os.getenv("RAG_INGESTION_URL", "http://localhost:8004")
_SUPPORTED = {".md", ".markdown", ".txt", ".pdf"}


def _iter_files(root: Path, rel_base: Path) -> list[tuple[Path, str]]:
    files: list[tuple[Path, str]] = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in _SUPPORTED:
            files.append((path, path.relative_to(rel_base).as_posix()))
    return files


async def sync(root: Path, *, reindex: bool, dry_run: bool) -> int:
    files = _iter_files(root, ROOT)
    if not files:
        print(f"[sync] {root} 下没有可同步的文件")
        return 0

    created = updated = skipped = failed = 0
    async with httpx.AsyncClient(timeout=600.0) as client:
        for path, rel_source in files:
            doc_id = stable_doc_id(rel_source)
            local_hash = content_hash(path.read_text(encoding="utf-8", errors="replace"))

            try:
                resp = await client.get(f"{INGESTION_URL}/documents/{doc_id}", timeout=30.0)
            except Exception as exc:  # noqa: BLE001
                print(f"[sync] 查询文档失败 {rel_source}: {exc}")
                failed += 1
                continue

            if resp.status_code == 404:
                action = "create"
            elif resp.status_code == 200 and resp.json().get("content_hash") != local_hash:
                action = "update"
            elif resp.status_code == 200:
                skipped += 1
                print(f"[sync] 跳过（内容未变）{rel_source}")
                continue
            else:
                print(f"[sync] 查询返回 {resp.status_code}: {rel_source}")
                failed += 1
                continue

            if dry_run:
                print(f"[sync] (dry-run) 将 {action}: {rel_source}")
                continue

            payload = {"path": rel_source, "reindex": reindex or action == "update"}
            try:
                ingest_resp = await client.post(
                    f"{INGESTION_URL}/ingest", json=payload, timeout=600.0
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[sync] 接入失败 {rel_source}: {exc}")
                failed += 1
                continue

            if ingest_resp.status_code >= 400:
                print(
                    f"[sync] 接入失败 {rel_source}: {ingest_resp.status_code} {ingest_resp.text[:200]}"
                )
                failed += 1
                continue

            body = ingest_resp.json()
            if action == "create":
                created += 1
            else:
                updated += 1
            print(
                f"[sync] {action} {rel_source} chunks={body.get('chunk_count')} "
                f"indexed={body.get('indexed')}"
            )

    print(f"\n[sync] 新增 {created}，更新 {updated}，跳过 {skipped}，失败 {failed}")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="语料增量同步")
    parser.add_argument("--path", default="data/corpus")
    parser.add_argument("--reindex", action="store_true", help="强制重建索引")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不实际写入")
    args = parser.parse_args()
    return asyncio.run(sync(Path(args.path), reindex=args.reindex, dry_run=args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
