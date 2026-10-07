"""Postgres 元数据写入（文档台账 + 分块明细 + 接入任务）。

职责边界：ingestion 负责「文档与 ACL 落库」，indexing 只负责「检索索引落库」。
两者分开，避免同一个服务同时握有两种存储的事务语义。
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, func, or_, select

from packages.common.db import session_scope
from packages.common.logging import get_logger
from packages.common.models import Chunk as ChunkRow
from packages.common.models import Document as DocumentRow
from packages.common.models import IngestionJob, Tenant
from packages.contracts import Chunk, Document

logger = get_logger("ingestion.metadata")


class MetadataStore:
    def __init__(self, database_url: str) -> None:
        self._url = database_url

    # ---------------- 租户 ----------------
    async def ensure_tenant(self, tenant_id: str, name: str | None = None) -> None:
        async with session_scope(self._url) as session:
            exists = await session.get(Tenant, tenant_id)
            if exists is None:
                session.add(Tenant(id=tenant_id, name=name or tenant_id))

    # ---------------- 文档 ----------------
    async def upsert_document(self, doc: Document, *, status: str = "pending") -> None:
        size_bytes = int(doc.metadata.get("size_bytes") or len(doc.content.encode("utf-8")))
        async with session_scope(self._url) as session:
            row = await session.get(DocumentRow, doc.doc_id)
            if row is None:
                row = DocumentRow(doc_id=doc.doc_id)
                session.add(row)
            row.tenant_id = doc.acl.tenant_id
            row.department_id = doc.acl.department_id
            row.visibility = doc.acl.visibility.value
            row.title = doc.title
            row.source = doc.source
            row.content_hash = doc.content_hash
            row.size_bytes = size_bytes
            row.status = status
            row.meta = doc.metadata

    async def replace_chunks(self, doc_id: str, chunks: list[Chunk]) -> int:
        async with session_scope(self._url) as session:
            await session.execute(delete(ChunkRow).where(ChunkRow.doc_id == doc_id))
            session.add_all(
                [
                    ChunkRow(
                        chunk_id=c.chunk_id,
                        doc_id=c.doc_id,
                        chunk_index=c.chunk_index,
                        section_path=c.section_path,
                        char_start=c.char_start,
                        char_end=c.char_end,
                        token_count=c.token_count,
                        tenant_id=c.acl.tenant_id,
                        department_id=c.acl.department_id,
                        visibility=c.acl.visibility.value,
                    )
                    for c in chunks
                ]
            )
        return len(chunks)

    async def update_document_status(
        self, doc_id: str, status: str, chunk_count: int | None = None
    ) -> None:
        async with session_scope(self._url) as session:
            row = await session.get(DocumentRow, doc_id)
            if row is None:
                logger.warning("文档不存在，跳过状态更新 doc_id=%s", doc_id)
                return
            row.status = status
            if chunk_count is not None:
                row.chunk_count = chunk_count

    async def delete_document(self, doc_id: str) -> None:
        async with session_scope(self._url) as session:
            row = await session.get(DocumentRow, doc_id)
            if row is not None:
                # chunks 由 ORM 级联删除
                await session.delete(row)

    async def get_document(self, doc_id: str) -> dict | None:
        async with session_scope(self._url) as session:
            row = await session.get(DocumentRow, doc_id)
            if row is None:
                return None
            return {
                "doc_id": row.doc_id,
                "title": row.title,
                "source": row.source,
                "status": row.status,
                "content_hash": row.content_hash,
                "chunk_count": row.chunk_count,
                "tenant_id": row.tenant_id,
                "department_id": row.department_id,
                "visibility": row.visibility,
            }

    async def list_documents(
        self,
        *,
        tenant_id: str | None = None,
        keyword: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """分页列出文档台账。

        关键字同时匹配标题 / 来源路径 / doc_id——管理界面上排查「这份文档到底进没进去」
        时，用来源路径搜是最常用的入口。
        """
        stmt = select(DocumentRow)
        count_stmt = select(func.count()).select_from(DocumentRow)

        if tenant_id:
            stmt = stmt.where(DocumentRow.tenant_id == tenant_id)
            count_stmt = count_stmt.where(DocumentRow.tenant_id == tenant_id)
        if keyword:
            like = f"%{keyword}%"
            condition = or_(
                DocumentRow.title.ilike(like),
                DocumentRow.source.ilike(like),
                DocumentRow.doc_id.ilike(like),
            )
            stmt = stmt.where(condition)
            count_stmt = count_stmt.where(condition)

        stmt = stmt.order_by(DocumentRow.updated_at.desc()).limit(limit).offset(offset)

        async with session_scope(self._url) as session:
            rows = (await session.scalars(stmt)).all()
            total = await session.scalar(count_stmt)

        return {
            "total": int(total or 0),
            "limit": limit,
            "offset": offset,
            "items": [
                {
                    "doc_id": row.doc_id,
                    "title": row.title,
                    "source": row.source,
                    "status": row.status,
                    "chunk_count": row.chunk_count,
                    "size_bytes": row.size_bytes,
                    "tenant_id": row.tenant_id,
                    "department_id": row.department_id,
                    "visibility": row.visibility,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                    "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                }
                for row in rows
            ],
        }

    # ---------------- 接入任务 ----------------
    async def create_job(self, source: str) -> str:
        job_id = uuid.uuid4()
        async with session_scope(self._url) as session:
            session.add(IngestionJob(id=job_id, source=source, status="running"))
        return str(job_id)

    async def update_job(
        self,
        job_id: str,
        *,
        status: str,
        doc_id: str | None = None,
        chunk_count: int = 0,
        message: str | None = None,
    ) -> None:
        async with session_scope(self._url) as session:
            row = await session.get(IngestionJob, uuid.UUID(job_id))
            if row is None:
                return
            row.status = status
            row.doc_id = doc_id or row.doc_id
            row.chunk_count = chunk_count or row.chunk_count
            row.message = message

    async def get_job(self, job_id: str) -> dict | None:
        async with session_scope(self._url) as session:
            row = await session.get(IngestionJob, uuid.UUID(job_id))
            if row is None:
                return None
            return {
                "job_id": str(row.id),
                "source": row.source,
                "status": row.status,
                "doc_id": row.doc_id,
                "chunk_count": row.chunk_count,
                "message": row.message,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }

    # ---------------- 观测 ----------------
    async def stats(self) -> dict[str, int]:
        async with session_scope(self._url) as session:
            docs = await session.scalar(select(func.count()).select_from(DocumentRow))
            chunks = await session.scalar(select(func.count()).select_from(ChunkRow))
        return {"documents": int(docs or 0), "chunks": int(chunks or 0)}

    async def health(self) -> tuple[bool, str]:
        try:
            stats = await self.stats()
            return True, f"documents={stats['documents']} chunks={stats['chunks']}"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
