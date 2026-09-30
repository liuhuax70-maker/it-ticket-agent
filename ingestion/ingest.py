"""知识库入库脚本。

流程：读取源文档 → 切分（`ingestion.chunk`）→ 向量化（Ollama）→ **upsert** 写入 Milvus。

目录约定：

    knowledge_base/
    ├── manual/*.md        → source=manual（产品手册）
    ├── faq/*.md           → source=faq（常见问题）
    └── tickets/*.jsonl    → source=ticket（历史工单，每行一条）

**幂等**：写入用 `upsert`（主键 = `chunk_id`），重复执行不会产生重复片段；
文档内容变化导致 `chunk_id` 变化时，旧片段可用 `--drop-source` 清理。

**一致性**：非重建模式下会先 `verify_index_consistency()`，
若索引里的 embedding 模型/维度与当前配置不一致，直接拒绝写入（防止静默劣化）。

运行：

    python -m ingestion.ingest                        # 增量 upsert
    python -m ingestion.ingest --recreate             # 重建集合（清空并写入）
    python -m ingestion.ingest --drop-source manual   # 先删除某来源，再写入
    python -m ingestion.ingest --chunk-size 300 --overlap 0
"""

import argparse
import json
import time
from pathlib import Path

from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging
from app.retrieval.dense import embed_batched
from app.retrieval.milvus_store import (
    FIELD_CHUNK_ID,
    FIELD_CONTENT,
    FIELD_CONTENT_HASH,
    FIELD_DENSE,
    FIELD_DOC_ID,
    FIELD_EMBEDDING_MODEL,
    FIELD_ERROR_CODE,
    FIELD_HEADING_PATH,
    FIELD_SOURCE,
    FIELD_TITLE,
    FIELD_UPDATED_AT,
    FIELD_VERSION,
    count_by_source,
    delete_by_source,
    ensure_collection,
    upsert_rows,
    verify_index_consistency,
)
from app.schemas.retrieval import DocSource
from ingestion.chunk import (
    DEFAULT_CHUNK_TOKENS,
    DEFAULT_OVERLAP_TOKENS,
    RawChunk,
    dedupe_chunks,
    extract_metadata,
    split_document,
)

logger = get_logger(__name__)

#: 默认知识库根目录（相对仓库根）
DEFAULT_KB_PATH = Path("knowledge_base")

#: 目录名 → 知识来源类型
SOURCE_DIRS: dict[str, DocSource] = {
    "manual": DocSource.MANUAL,
    "faq": DocSource.FAQ,
    "tickets": DocSource.TICKET,
}

#: content 字段上限（与 milvus_store.MAX_CONTENT_LENGTH 保持一致，留出安全余量）
MAX_CONTENT_CHARS = 8000
#: title 字段上限
MAX_TITLE_CHARS = 200
#: heading_path 字段上限
MAX_PATH_CHARS = 500


def _truncate(text: str, limit: int, *, what: str, chunk_id: str) -> str:
    """超长字段截断并告警（Milvus VARCHAR 超限会直接写入失败）。"""
    if len(text) <= limit:
        return text
    logger.warning("%s 超长已截断: %s (%d → %d)", what, chunk_id, len(text), limit)
    return text[:limit]


def load_markdown(
    path: Path,
    source: DocSource,
    *,
    chunk_size: int = DEFAULT_CHUNK_TOKENS,
    overlap: int = DEFAULT_OVERLAP_TOKENS,
) -> list[RawChunk]:
    """加载单个 Markdown 文档并切分。"""
    text = path.read_text(encoding="utf-8")
    doc_id = f"{source.value}-{path.stem}"
    chunks = split_document(doc_id, text, chunk_size=chunk_size, overlap=overlap, title=path.stem)
    for chunk in chunks:
        chunk.metadata["source"] = source.value
    return chunks


def load_tickets(path: Path) -> list[RawChunk]:
    """加载历史工单 JSONL（每行一条 question/answer）。"""
    chunks: list[RawChunk] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            logger.warning("%s 第 %d 行 JSON 解析失败，已跳过: %s", path.name, lineno, exc)
            continue

        question = (record.get("question") or "").strip()
        answer = (record.get("answer") or "").strip()
        if not question or not answer:
            logger.warning("%s 第 %d 行缺少 question/answer，已跳过", path.name, lineno)
            continue

        doc_id = record.get("ticket_id") or f"{path.stem}-{lineno}"
        heading_path = (f"历史工单 > {question[:40]}",)
        content = f"【{heading_path[0]}】\n问题：{question}\n\n答复：{answer}"
        meta = extract_metadata(content)
        chunks.append(
            RawChunk(
                doc_id=doc_id,
                chunk_index=0,
                content=content,
                title=question[:MAX_TITLE_CHARS],
                heading_path=heading_path,
                version=meta["version"],
                error_code=meta["error_code"],
                metadata={"source": DocSource.TICKET.value},
            )
        )
    return chunks


def load_knowledge_base(
    root: Path,
    *,
    chunk_size: int = DEFAULT_CHUNK_TOKENS,
    overlap: int = DEFAULT_OVERLAP_TOKENS,
) -> list[RawChunk]:
    """遍历知识库目录，返回全部片段。"""
    if not root.is_dir():
        raise FileNotFoundError(f"知识库目录不存在: {root.resolve()}")

    chunks: list[RawChunk] = []
    for dir_name, source in SOURCE_DIRS.items():
        source_dir = root / dir_name
        if not source_dir.is_dir():
            logger.warning("跳过不存在的目录: %s", source_dir)
            continue

        pattern = "*.jsonl" if dir_name == "tickets" else "*.md"
        for path in sorted(source_dir.glob(pattern)):
            loaded = (
                load_tickets(path)
                if dir_name == "tickets"
                else load_markdown(path, source, chunk_size=chunk_size, overlap=overlap)
            )
            logger.info("加载 %s: %s → %d 个片段", source.value, path.name, len(loaded))
            chunks.extend(loaded)

    return chunks


def to_row(chunk: RawChunk, vector: list[float], source: DocSource, embedding_model: str) -> dict:
    """RawChunk + 向量 → Milvus 写入行。"""
    return {
        FIELD_CHUNK_ID: chunk.chunk_id,
        FIELD_DOC_ID: chunk.doc_id,
        FIELD_CONTENT: _truncate(
            chunk.content, MAX_CONTENT_CHARS, what="content", chunk_id=chunk.chunk_id
        ),
        FIELD_DENSE: vector,
        # sparse 由 Milvus 的 BM25 Function 自动生成，无需提供
        FIELD_SOURCE: source.value,
        FIELD_TITLE: _truncate(
            chunk.title or "", MAX_TITLE_CHARS, what="title", chunk_id=chunk.chunk_id
        ),
        FIELD_HEADING_PATH: _truncate(
            chunk.heading_path_text, MAX_PATH_CHARS, what="heading_path", chunk_id=chunk.chunk_id
        ),
        FIELD_CONTENT_HASH: chunk.content_hash,
        FIELD_EMBEDDING_MODEL: embedding_model,
        FIELD_VERSION: chunk.version or "",
        FIELD_ERROR_CODE: chunk.error_code or "",
        FIELD_UPDATED_AT: int(time.time()),
    }


def _resolve_source(chunk: RawChunk) -> DocSource:
    """读取片段携带的来源标记（加载阶段写入 metadata.source）。"""
    raw = chunk.metadata.get("source")
    if raw:
        try:
            return DocSource(raw)
        except ValueError:
            logger.warning("未知来源 %r（chunk=%s），按 ticket 处理", raw, chunk.chunk_id)
    return DocSource.TICKET


def ingest(
    root: Path = DEFAULT_KB_PATH,
    *,
    recreate: bool = False,
    drop_sources: tuple[str, ...] = (),
    chunk_size: int = DEFAULT_CHUNK_TOKENS,
    overlap: int = DEFAULT_OVERLAP_TOKENS,
) -> dict:
    """执行一次知识库导入，返回统计信息。"""
    settings = get_settings()

    ensure_collection(recreate=recreate)

    if not recreate:
        # 索引里的 embedding 模型必须与当前配置一致，否则拒绝写入
        verify_index_consistency()

    for source in drop_sources:
        delete_by_source(source)

    chunks = dedupe_chunks(load_knowledge_base(root, chunk_size=chunk_size, overlap=overlap))
    if not chunks:
        logger.warning("没有可导入的片段，已跳过")
        return {"loaded": 0, "upserted": 0, "by_source": count_by_source()}

    logger.info(
        "共 %d 个片段，开始向量化（模型 %s，chunk_size=%d，overlap=%d）...",
        len(chunks),
        settings.embedding_model,
        chunk_size,
        overlap,
    )
    vectors = embed_batched([chunk.content for chunk in chunks])

    rows = [
        to_row(chunk, vector, _resolve_source(chunk), settings.embedding_model)
        for chunk, vector in zip(chunks, vectors, strict=True)
    ]

    upserted = upsert_rows(rows)
    logger.info("写入完成（upsert）: %d 行", upserted)

    return {
        "loaded": len(chunks),
        "upserted": upserted,
        "chunk_size": chunk_size,
        "overlap": overlap,
        "embedding_model": settings.embedding_model,
        "by_source": count_by_source(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="知识库入库")
    parser.add_argument("--path", type=Path, default=DEFAULT_KB_PATH, help="知识库根目录")
    parser.add_argument("--recreate", action="store_true", help="重建集合（会清空已有数据）")
    parser.add_argument(
        "--drop-source",
        action="append",
        default=[],
        help="写入前先删除指定来源（manual/faq/ticket），可重复",
    )
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_TOKENS, help="单块 token 预算")
    parser.add_argument("--overlap", type=int, default=DEFAULT_OVERLAP_TOKENS, help="重叠 token 数")
    args = parser.parse_args()

    setup_logging(get_settings().log_level)
    stats = ingest(
        args.path,
        recreate=args.recreate,
        drop_sources=tuple(args.drop_source),
        chunk_size=args.chunk_size,
        overlap=args.overlap,
    )
    logger.info("入库结束: %s", stats)
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
