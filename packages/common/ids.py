"""ID 生成。

铁律：``doc_id`` 必须由 ``source`` 稳定派生，而不是随机 UUID——
否则重跑索引会产生重复文档（旧 P0 坑位 #7）。
"""

from __future__ import annotations

import hashlib
import uuid


def stable_doc_id(source: str) -> str:
    """由来源稳定派生 doc_id。"""
    digest = hashlib.sha1(source.encode("utf-8")).hexdigest()[:8]
    return f"d_{digest}"


def stable_chunk_id(doc_id: str, chunk_index: int) -> str:
    return f"{doc_id}:{chunk_index}"


def content_hash(text: str) -> str:
    """内容指纹，用于增量判断是否需要重建索引。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def new_id(prefix: str = "") -> str:
    value = uuid.uuid4().hex
    return f"{prefix}{value}" if prefix else value


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()
