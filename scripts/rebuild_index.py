"""重建检索索引：Milvus 集合 + 从语料重新入库。

**给向量集合加字段时必须走这里**：Milvus schema 没有 alter（``_ensure_collection_sync``
只"不存在才建"），新增标量字段对已有集合不生效、写入时会被拒。切分参数变更同样属于换
ID 空间（见 :func:`packages.common.ids.stable_chunk_id`），也需重建。

用法：
    python scripts/rebuild_index.py# 重建 Milvus 集合并重新入库全部语料
    python scripts/rebuild_index.py --dry-run      # 只打印将要做的事

前提是语料在仓库里（``data/corpus``、``data/corpus_permissions``）且索引可完全由它们重建——
语料不完整时重建会造成数据丢失，所以脚本动手前先确认语料可读。
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
    """删（或保留）Milvus 集合后重新入库全部语料。

    入口先校验语料完整性——索引完全由语料重建，语料缺失时 drop 集合
    等于不可恢复地丢数据，因此"语料为空"直接拒绝而非冒险执行。
    """
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
