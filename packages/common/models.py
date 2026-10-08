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
    """租户表：多租户隔离的根，文档/分块/反馈/审计都按 tenant_id 归属。"""

    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    config: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )


class Document(Base, TimestampMixin):
    """文档台账：元数据、ACL、状态与分块数。

    知识库管理界面（``GET /documents``）据此展示标题/来源/状态/分块数/大小/更新时间；
    ``meta`` 列存放业务自定义元数据（注意不能叫 ``metadata``，那是 DeclarativeBase 保留名）。
    """

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
    """分块元数据表：定位信息 + ACL 冗余列。

    ⚠️ 这里的 ``tenant_id/department_id/visibility`` 是**写入时**随 chunk 落库的冗余列，
    当前没有任何读取点——检索侧读的是 Milvus / OpenSearch 里各自的冗余字段，
    并没有代码比对这三列与索引侧是否一致。它们是"写而不读"的历史包袱，
    不要以为"元数据与索引的一致性"已被保障；未来若做一致性校验，这三列即比对基准。
    """

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
    # ACL 冗余列：写入时随 chunk 一起落库。
    # ⚠️ 当前**没有任何读取点**——检索侧读的是 Milvus / OpenSearch 里各自的冗余字段，
    # 并没有任何代码比对这三列与索引侧是否一致。所以它们现在是"写而不读"的历史包袱，
    # 不要以为"元数据与索引的一致性"已被保障。
    # 未来若要做一致性校验（写入后抽样比对），这三列就是比对基准。
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    department_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    visibility: Mapped[str] = mapped_column(String(32), nullable=False, default="internal")

    document: Mapped[Document] = relationship(back_populates="chunks")

    __table_args__ = (Index("ix_chunks_doc_index", "doc_id", "chunk_index"),)


class IngestionJob(Base, TimestampMixin):
    """接入任务表：跟踪单次接入的来源、状态、分块数与失败信息。"""

    __tablename__ = "ingestion_jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    doc_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Feedback(Base, TimestampMixin):
    """用户反馈表：点赞/点踩评级与留言，按租户 + trace_id 关联（用于坏例导出）。"""

    __tablename__ = "feedback"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    query: Mapped[str] = mapped_column(Text, nullable=False, default="")
    answer: Mapped[str] = mapped_column(Text, nullable=False, default="")
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AuditLog(Base, TimestampMixin):
    """访问审计（合规留痕）。

    为什么落库而不是只打日志：stdout 日志会随容器重建而消失、无法按租户/用户检索，
    合规审查要的是「谁在什么时候访问了什么、结果如何」**可回溯**。
    字段与 AuditMiddleware 组装的 record 一一对应；
    **不含请求体与答案正文**——审计日志自身不能成为泄密面。
    """

    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 事件类型（http_access / ...）。中间件的 record 与这里的字段必须一一对应：
    # 多出的键会让批量写入整批失败（已实测），sink 会丢弃并告警。
    event: Mapped[str] = mapped_column(String(32), nullable=False, default="http_access")
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    duration_ms: Mapped[float] = mapped_column(nullable=False, default=0.0)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    client: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    __table_args__ = (Index("ix_audit_created", "created_at"),)
