"""重建检索索引：Milvus 集合 + 从语料重新入库。

什么时候需要它：
    **给向量集合加字段的时候。** Milvus 的 schema 没有 alter（`_ensure_collection_sync`
    是"不存在才建"），新增标量字段对已有集合**不会生效**，写入时会被拒。
    所以加字段 = 重建集合 = 重新入库，这条路径必须存在且可执行。

    同理，切分参数（chunk_size/overlap）改变也属于换 ID 空间，需要重建（见 README）。

用法：
    python scripts/rebuild_index.py                # 重建 Milvus 集合并重新入库全部语料
    python scripts/rebuild_index.py --dry-run      # 只打印将要做的事

前置条件：语料在仓库里（`data/corpus`、`data/corpus_permissions`），
索引可以完全由它们重建。若语料不完整，重建会造成数据丢失——
因此脚本默认**先确认当前语料可读**，再动手。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CORPUS_DIRS = (REPO_ROOT / "data" / "corpus", REPO_ROOT / "data" / "corpus_permissions")


def _prepare(dry_run: bool) -> int:
    """调用语料准备脚本重新入库（上传 + 重建索引一次做完）。"""
    if dry_run:
        print("  将执行：python scripts/prepare_corpus.py")
        return 0
    result = subprocess.run([sys.executable, "scripts/prepare_corpus.py"], cwd=REPO_ROOT, text=True)
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="重建检索索引")
    parser.add_argument(
        "--keep-vector-collection",
        action="store_true",
        help="不删 Milvus 集合（只重新入库；加字段时**必须**删，否则新字段无效）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印将要做的事")
    args = parser.parse_args()

    # 先确认语料可读：索引由语料重建，语料不全就等于丢数据
    files = [p for d in CORPUS_DIRS for p in sorted(d.glob("*.md"))]
    if not files:
        print(f"语料为空（{[str(d) for d in CORPUS_DIRS]} 下没有 .md）——拒绝重建，否则等于删库")
        return 1
    print(f"语料可读：{len(files)} 篇，索引可由语料完全重建")

    from pymilvus import MilvusClient

    from packages.vectorstores.config import MilvusSettings

    settings = MilvusSettings()
    client = MilvusClient(uri=settings.milvus_uri, timeout=settings.milvus_timeout)
    exists = client.has_collection(settings.milvus_collection)

    if args.keep_vector_collection:
        print("  跳过删除 Milvus 集合（--keep-vector-collection）")
    elif not exists:
        print(f"  Milvus 集合 {settings.milvus_collection} 不存在，无需删除")
    else:
        row_count = client.get_collection_stats(settings.milvus_collection).get("row_count")
        print(f"  将删除 Milvus 集合 {settings.milvus_collection}（{row_count} 行，由语料重建）")
        if not args.dry_run:
            client.drop_collection(settings.milvus_collection)
            print("  已删除")

    print()
    print("== 重新入库 ==")
    return _prepare(args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
