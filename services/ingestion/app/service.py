"""接入编排：解析 -> 切分 -> 落元数据 -> 同步直连入库。

顺序（仅同步直连模式成立）：先写 documents 行（status=pending）与 chunks 明细，再调
indexing 写索引，只有成功才把 status 置 indexed——这样「元数据说已索引」永远意味着索引
真的写了，不会出现半成品状态。

两种模式语义不同（``USE_KAFKA`` 切换）：HTTP 直连下 ``send()`` 返回时索引已写完；Kafka 下
``send()`` 只表示事件投递成功，status=indexed 的含义是"已投递"、检索可见性最终一致，
按 status 判断"能不能检索到"只在直连模式下有效。

已知缺口：直连模式最后一步只判断调用是否抛异常，**没有区分 indexing 返回的 partial 状态**
（例如 Milvus 成功而 OpenSearch 失败）。
"""

from __future__ import annotations

import time
from pathlib import Path

from app.chunkers import ChunkingConfig, RecursiveChunker
from app.config import Settings
from app.metadata import MetadataStore
from app.parsers import all_extensions, get_parser
from app.producers import KafkaChunkSink, build_sink
from packages.common.constants import LIFECYCLE_ACTIVE, LIFECYCLE_RETIRED
from packages.common.errors import DependencyUnavailable, NotFoundError, ValidationError
from packages.common.ids import content_hash, stable_doc_id
from packages.common.lifecycle import load_declarations, resolve
from packages.common.logging import get_logger
from packages.contracts import ACL, Document, IngestRequest, IngestResponse, Visibility

logger = get_logger("ingestion.service")


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


class IngestionService:
    """接入编排：解析→切分→落元数据→同步直连入库。

    核心不变量（见模块 docstring）：直连模式下 ``status=indexed`` 意味着索引已真实写入；
    Kafka 模式下它只表示「已投递」，检索可见性最终一致。
    """

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
        # 生命周期声明在启动时读一次：入库期间改文件不会自动生效，
        # 这与"改调参文件必须重启服务"是同一条约束（见 configs/retrievers 的说明）。
        self._lifecycle = load_declarations(settings.lifecycle_config_path)

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
        # 生命周期按**文件名**匹配声明：上传入库的文件其 source 会被改写成
        # data/uploads/<原名>，按完整路径匹配必然对不上。
        state = resolve(Path(source).name, self._lifecycle)
        metadata: dict[str, object] = {
            "size_bytes": len(text.encode("utf-8")),
            "lifecycle": state.lifecycle,
            "lifecycle_reason": state.reason,
        }
        if state.details:
            # 生效/失效日期与被谁取代——供台账与人工查看，不参与过滤
            metadata["lifecycle_details"] = dict(state.details)
        if state.lifecycle == LIFECYCLE_RETIRED:
            logger.info("文档已废止，入库后不参与检索: %s（%s）", source, state.reason)

        return Document(
            doc_id=stable_doc_id(source),
            source=source,
            title=resolved_title or Path(source).stem,
            content=text,
            content_hash=content_hash(text),
            acl=acl,
            metadata=metadata,
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
            any_failed = False

            for doc in documents:
                started = time.perf_counter()
                chunks = self._chunker.chunk(doc)
                if not chunks:
                    logger.warning("文档切分结果为空，跳过: %s", doc.source)
                    continue
                # 生命周期落到**每个分块**上：检索过滤下推到存储层，
                # 而存储层看到的是分块，不是文档——漏了这一步，废止状态就只是元数据。
                lifecycle = str(doc.metadata.get("lifecycle") or LIFECYCLE_ACTIVE)
                for chunk in chunks:
                    chunk.lifecycle = lifecycle
                timings["chunk"] = timings.get("chunk", 0.0) + _ms(started)

                started = time.perf_counter()
                await self._metadata.upsert_document(doc, status="pending")
                await self._metadata.replace_chunks(doc.doc_id, chunks)
                timings["metadata"] = timings.get("metadata", 0.0) + _ms(started)

                started = time.perf_counter()
                try:
                    result = await self._sink.send(chunks, reindex=req.reindex)
                except Exception as exc:  # noqa: BLE001
                    await self._metadata.update_document_status(doc.doc_id, "failed")
                    logger.error("索引写入失败 doc_id=%s err=%s", doc.doc_id, exc)
                    any_failed = True
                    if self._settings.fail_fast_on_index_error:
                        raise
                else:
                    # send 不抛异常也可能没写全（某库写 0 条时返回 status="partial"），
                    # 这种不能当成功——否则 Milvus/OpenSearch 一侧 0 条却对外称已索引。
                    if result.status == "ok":
                        await self._metadata.update_document_status(
                            doc.doc_id, "indexed", len(chunks)
                        )
                        indexed += len(chunks)
                        total_chunks += len(chunks)
                        doc_ids.append(doc.doc_id)
                    else:
                        await self._metadata.update_document_status(doc.doc_id, "failed")
                        logger.error(
                            "索引部分写入失败 doc_id=%s milvus=%s opensearch=%s",
                            doc.doc_id,
                            result.milvus,
                            result.opensearch,
                        )
                        any_failed = True
                        if self._settings.fail_fast_on_index_error:
                            raise RuntimeError(f"索引部分写入失败 doc_id={doc.doc_id}")
                timings["index"] = timings.get("index", 0.0) + _ms(started)

            job_status = "failed" if any_failed else "succeeded"
            await self._metadata.update_job(
                job_id, status=job_status, chunk_count=total_chunks, message=None
            )
            logger.info(
                "接入完成 job=%s docs=%s chunks=%s indexed=%s failed=%s",
                job_id,
                len(doc_ids),
                total_chunks,
                indexed,
                any_failed,
            )
            return IngestResponse(
                job_id=job_id,
                status=job_status,
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

    # ---------------- 台账查询 ----------------
    async def list_documents(
        self,
        *,
        tenant_id: str | None = None,
        keyword: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        return await self._metadata.list_documents(
            tenant_id=tenant_id, keyword=keyword, limit=limit, offset=offset
        )

    async def get_document(self, doc_id: str) -> dict | None:
        return await self._metadata.get_document(doc_id)

    async def get_job(self, job_id: str) -> dict | None:
        return await self._metadata.get_job(job_id)

    async def stats(self) -> dict[str, int]:
        return await self._metadata.stats()

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
