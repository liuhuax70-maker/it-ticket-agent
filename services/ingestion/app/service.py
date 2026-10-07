"""接入编排：解析 -> 切分 -> 落元数据 -> 同步直连入库。

顺序设计（同步直连模式下）：
    1. 先建/更新 documents 行（status=pending）与 chunks 明细；
    2. 再调用 indexing 写检索索引；
    3. 只有第 2 步成功才把 status 置为 indexed。
这样任何时刻「元数据说已索引」都意味着索引真的写了，不会出现半成品状态。
"""

from __future__ import annotations

import time
from pathlib import Path

from app.chunkers import ChunkingConfig, RecursiveChunker
from app.config import Settings
from app.metadata import MetadataStore
from app.parsers import all_extensions, get_parser
from app.producers import KafkaChunkSink, build_sink
from packages.common.errors import DependencyUnavailable, NotFoundError, ValidationError
from packages.common.ids import content_hash, stable_doc_id
from packages.common.logging import get_logger
from packages.contracts import ACL, Document, IngestRequest, IngestResponse, Visibility

logger = get_logger("ingestion.service")


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


class IngestionService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._chunker = RecursiveChunker(
            ChunkingConfig(
                chunk_size=settings.chunk_size,
                chunk_overlap=settings.chunk_overlap,
                min_chunk_size=settings.min_chunk_size,
            )
        )
        self._metadata = MetadataStore(settings.database_url)
        self._sink = build_sink(
            use_kafka=settings.use_kafka,
            indexing_url=settings.indexing_url,
            timeout=settings.indexing_timeout,
            bootstrap=settings.kafka_bootstrap,
            topic=settings.kafka_topic_chunk_events,
        )

    # ---------------- 生命周期 ----------------
    async def startup(self) -> None:
        ok, message = await self._metadata.health()
        if not ok:
            raise DependencyUnavailable("Postgres", f"元数据库不可用: {message}")
        await self._metadata.ensure_tenant(self._settings.default_tenant_id, "默认租户")
        if isinstance(self._sink, KafkaChunkSink):
            await self._sink.start()
        logger.info(
            "ingestion 就绪: docs_dir=%s chunk=%s/%s sink=%s",
            self._settings.docs_dir,
            self._settings.chunk_size,
            self._settings.chunk_overlap,
            type(self._sink).__name__,
        )

    async def aclose(self) -> None:
        await self._sink.aclose()

    # ---------------- 文档装载 ----------------
    def _default_acl(self, override: ACL | None) -> ACL:
        if override is not None:
            return override
        return ACL(
            tenant_id=self._settings.default_tenant_id,
            department_id=self._settings.default_department_id,
            visibility=Visibility.internal,
        )

    def _document_from_text(self, text: str, source: str, title: str | None, acl: ACL) -> Document:
        resolved_title = title or ""
        if not resolved_title:
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    resolved_title = stripped.lstrip("#").strip()
                    break
                if stripped:
                    resolved_title = stripped[:120]
                    break
        return Document(
            doc_id=stable_doc_id(source),
            source=source,
            title=resolved_title or Path(source).stem,
            content=text,
            content_hash=content_hash(text),
            acl=acl,
            metadata={"size_bytes": len(text.encode("utf-8"))},
        )

    def _documents_from_request(self, req: IngestRequest, acl: ACL) -> list[Document]:
        if req.content is not None:
            source = f"inline://{req.filename or stable_doc_id(req.content)[2:]}"
            return [self._document_from_text(req.content, source, req.title, acl)]

        if not req.path:
            raise ValidationError("必须提供 content、path 之一")

        target = Path(req.path)
        if not target.exists():
            raise NotFoundError(f"路径不存在: {target}")

        if target.is_dir():
            files = [
                p
                for p in sorted(target.rglob("*"))
                if p.is_file() and p.suffix.lower() in all_extensions()
            ]
            if not files:
                raise ValidationError(
                    f"目录 {target} 下没有可解析的文件（支持: {sorted(all_extensions())}）"
                )
            docs = [self._document_from_file(p, acl) for p in files]
        else:
            docs = [self._document_from_file(target, acl)]
        return docs

    def _document_from_file(self, path: Path, acl: ACL) -> Document:
        size_mb = path.stat().st_size / (1024 * 1024)
        if size_mb > self._settings.max_file_size_mb:
            raise ValidationError(
                f"{path.name} 超过单文件上限 {self._settings.max_file_size_mb}MB（{size_mb:.1f}MB）"
            )
        parsed = get_parser(path).parse(path)
        # source 统一用 posix 分隔符：doc_id 由 source 派生，若分隔符随平台变化，
        # Windows 与 Linux 上同一文件会得到不同 doc_id，增量同步与引用回查都会错位。
        return self._document_from_text(parsed.text, path.as_posix(), parsed.title, acl)

    # ---------------- 主流程 ----------------
    async def ingest(self, req: IngestRequest) -> IngestResponse:
        acl = self._default_acl(req.acl)
        job_id = await self._metadata.create_job(req.path or req.filename or "inline")

        timings: dict[str, float] = {}
        try:
            started = time.perf_counter()
            documents = self._documents_from_request(req, acl)
            timings["parse"] = _ms(started)

            total_chunks = 0
            indexed = 0
            doc_ids: list[str] = []

            for doc in documents:
                started = time.perf_counter()
                chunks = self._chunker.chunk(doc)
                if not chunks:
                    logger.warning("文档切分结果为空，跳过: %s", doc.source)
                    continue
                timings["chunk"] = timings.get("chunk", 0.0) + _ms(started)

                started = time.perf_counter()
                await self._metadata.upsert_document(doc, status="pending")
                await self._metadata.replace_chunks(doc.doc_id, chunks)
                timings["metadata"] = timings.get("metadata", 0.0) + _ms(started)

                started = time.perf_counter()
                try:
                    result = await self._sink.send(chunks, reindex=req.reindex)
                    await self._metadata.update_document_status(doc.doc_id, "indexed", len(chunks))
                    indexed += result.chunks_indexed
                except Exception as exc:  # noqa: BLE001
                    await self._metadata.update_document_status(doc.doc_id, "failed")
                    logger.error("索引写入失败 doc_id=%s err=%s", doc.doc_id, exc)
                    if self._settings.fail_fast_on_index_error:
                        raise
                timings["index"] = timings.get("index", 0.0) + _ms(started)

                total_chunks += len(chunks)
                doc_ids.append(doc.doc_id)

            await self._metadata.update_job(
                job_id, status="succeeded", chunk_count=total_chunks, message=None
            )
            logger.info(
                "接入完成 job=%s docs=%s chunks=%s indexed=%s",
                job_id,
                len(doc_ids),
                total_chunks,
                indexed,
            )
            return IngestResponse(
                job_id=job_id,
                status="succeeded",
                documents=len(doc_ids),
                chunk_count=total_chunks,
                indexed=indexed,
                doc_ids=doc_ids,
                timings_ms=timings,
            )
        except Exception as exc:
            await self._metadata.update_job(job_id, status="failed", message=str(exc))
            raise

    async def delete_document(self, doc_id: str) -> dict[str, object]:
        """合规删除：先清检索索引，再清元数据。

        顺序不能反——元数据先删会让索引清理失去 doc_id 依据，
        留下「检索得到但查不到来源」的孤儿向量。删除是幂等的。
        """
        document = await self._metadata.get_document(doc_id)
        index_counts = await self._sink.delete_document(doc_id)
        await self._metadata.delete_document(doc_id)
        logger.info("已删除文档 doc_id=%s index=%s", doc_id, index_counts)
        return {"doc_id": doc_id, "deleted": index_counts, "existed": document is not None}

    async def ingest_bytes(
        self, filename: str, data: bytes, *, acl: ACL | None = None, reindex: bool = False
    ) -> IngestResponse:
        """上传入口：落盘后复用文件解析链路，保证与目录导入完全同构。"""
        safe_name = Path(filename).name
        if not safe_name:
            raise ValidationError("上传文件名非法")
        target = Path(self._settings.upload_dir) / safe_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        logger.info("已保存上传文件: %s (%s bytes)", target, len(data))
        return await self.ingest(IngestRequest(path=str(target), acl=acl, reindex=reindex))

    # ---------------- 观测 ----------------
    async def health(self) -> dict[str, str]:
        ok_pg, msg_pg = await self._metadata.health()
        ok_sink, msg_sink = await self._sink.ping()
        return {
            "status": "ok" if (ok_pg and ok_sink) else "degraded",
            "postgres": msg_pg,
            "sink": msg_sink,
            "supported": ",".join(sorted(all_extensions())),
            "docs_dir": self._settings.docs_dir,
        }
