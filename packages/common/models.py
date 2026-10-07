"""Postgres ORM 模型（文档元数据 / ACL / 任务 / 反馈）。

P0（旧设计）曾用 manifest.json 代替关系库；本项目栈内确定使用 Postgres，
因此元数据与 ACL 落库，Milvus / OpenSearch 只存检索所需的冗余字段。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Tenant(Base, TimestampMixin):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    config: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )


class Document(Base, TimestampMixin):
    __tablename__ = "documents"

    doc_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    department_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    visibility: Mapped[str] = mapped_column(String(32), nullable=False, default="internal")
    title: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    source: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="indexed")
    # 注意：不能叫 metadata（DeclarativeBase 保留名）
    meta: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    chunks: Mapped[list[Chunk]] = relationship(  # noqa: F821
        back_populates="document", cascade="all, delete-orphan", lazy="selectin"
    )


class Chunk(Base, TimestampMixin):
    __tablename__ = "chunks"

    chunk_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    doc_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("documents.doc_id", ondelete="CASCADE"), nullable=False, index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    section_path: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    char_start: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # ACL 冗余，用于检索侧一致性校验
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    department_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    visibility: Mapped[str] = mapped_column(String(32), nullable=False, default="internal")

    document: Mapped[Document] = relationship(back_populates="chunks")

    __table_args__ = (Index("ix_chunks_doc_index", "doc_id", "chunk_index"),)


class IngestionJob(Base, TimestampMixin):
    __tablename__ = "ingestion_jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    doc_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Feedback(Base, TimestampMixin):
    __tablename__ = "feedback"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    query: Mapped[str] = mapped_column(Text, nullable=False, default="")
    answer: Mapped[str] = mapped_column(Text, nullable=False, default="")
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
