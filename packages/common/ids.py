"""ID 生成。

``doc_id`` 必须由 ``source`` 稳定派生而非随机 UUID，否则重跑索引会产生重复文档。
"""

from __future__ import annotations

import hashlib
import uuid


def stable_doc_id(source: str) -> str:
    """由来源稳定派生 doc_id（``d_`` + sha1 前 8 位十六进制）。

    ``source`` 必须是 **posix 归一化路径**（正斜杠），归一化由接入层负责。传反斜杠路径
    会派生出与正斜杠不同的 doc_id，同一份文件在台账里出现两次、引用回查指向不存在的元数据。

    只取 8 位十六进制是为让 doc_id 短且可肉眼比对；按生日界约 6.5 万篇时碰撞概率达 50%，
    当前百篇级安全，扩到十万篇以上需改长度并规划迁移（改长度等于换 ID 空间）。
    """
    digest = hashlib.sha1(source.encode("utf-8")).hexdigest()[:8]
    return f"d_{digest}"


def stable_chunk_id(doc_id: str, chunk_index: int) -> str:
    """``{doc_id}:{chunk_index}``，同时作为 Milvus 主键与 OpenSearch ``_id``——
    幂等性的全部依赖：重跑索引写同一主键即覆盖而非追加。

    ``chunk_index`` 参与 ID，所以改 ``chunk_size`` / ``overlap`` 等于换 ID 空间，
    旧索引行不会被覆盖而残留成检索不到的孤儿，必须走 reindex（先按 doc_id purge 再写）。
    """
    return f"{doc_id}:{chunk_index}"


def content_hash(text: str) -> str:
    """内容指纹，用于增量判断是否需要重建索引。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def new_id(prefix: str = "") -> str:
    value = uuid.uuid4().hex
    return f"{prefix}{value}" if prefix else value


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()