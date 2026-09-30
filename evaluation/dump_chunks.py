"""导出知识库全部片段，便于维护评估集（`evaluation/testset.jsonl`）的期望命中项。

用法：
    python -m evaluation.dump_chunks            # 打印摘要
    python -m evaluation.dump_chunks --full     # 打印全文
"""

import argparse

from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging
from app.retrieval.milvus_store import get_client

logger = get_logger(__name__)


def load_all_chunks() -> list[dict]:
    """读取集合内全部片段（按 chunk_id 排序）。"""
    settings = get_settings()
    client = get_client()
    client.flush(settings.milvus_collection)

    rows = client.query(
        collection_name=settings.milvus_collection,
        filter="chunk_id != ''",
        output_fields=["chunk_id", "doc_id", "title", "source", "version", "error_code", "content"],
        limit=200,
    )
    rows.sort(key=lambda row: row["chunk_id"])
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="导出知识库片段")
    parser.add_argument("--full", action="store_true", help="打印完整内容")
    parser.add_argument("--preview", type=int, default=70, help="摘要模式下的字符数")
    args = parser.parse_args()

    setup_logging(get_settings().log_level)
    rows = load_all_chunks()

    print(f"共 {len(rows)} 个片段\n")
    for row in rows:
        content = row["content"].replace("\n", " ")
        body = content if args.full else content[: args.preview] + ("…" if len(content) > args.preview else "")
        meta = []
        if row.get("version"):
            meta.append(f"ver={row['version']}")
        if row.get("error_code"):
            meta.append(f"err={row['error_code']}")
        print(f"{row['chunk_id']:<26s} [{row.get('source')}] {' '.join(meta)}")
        print(f"    title={row.get('title')}")
        print(f"    {body}")
        print()


if __name__ == "__main__":
    main()
