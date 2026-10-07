"""ID 生成。

铁律：``doc_id`` 必须由 ``source`` 稳定派生，而不是随机 UUID——
否则重跑索引会产生重复文档（旧 P0 坑位 #7）。
"""

from __future__ import annotations

import hashlib
import uuid


def stable_doc_id(source: str) -> str:
    """由来源稳定派生 doc_id（``d_`` + sha1 前 8 位十六进制）。

    ⚠️ 调用方必须先保证 ``source`` 是 **posix 归一化路径**（正斜杠分隔）。
    这个前置约定由接入层负责（见 ``services/ingestion/app/service.py`` 的路径归一化），
    但后果落在这里：

        * Windows 上传入反斜杠路径（``data\\corpus\\a.md``）会派生出与
          ``data/corpus/a.md`` **不同**的 doc_id —— 同一份文件在台账里出现两次，
          重复文档、且引用回查会指向不存在的那条元数据。

    为什么只取 8 位十六进制（32 bit）：为了让 doc_id 短、可读、便于在日志与
    citation 里肉眼比对。代价是理论上存在碰撞——按生日界，约 6.5 万篇文档时
    碰撞概率就到 50% 量级。当前知识库规模（百篇级）完全安全；
    若要扩到十万篇以上，必须先改这里的长度并规划迁移（改长度等于换 ID 空间，
    需要配合 reindex 重建）。
    """
    digest = hashlib.sha1(source.encode("utf-8")).hexdigest()[:8]
    return f"d_{digest}"


def stable_chunk_id(doc_id: str, chunk_index: int) -> str:
    """``{doc_id}:{chunk_index}``。

    这个 ID 同时是 Milvus 的主键与 OpenSearch 的 ``_id``，**幂等性的全部依赖**：
    重跑索引写同一个主键 = 覆盖，而不是追加。

    注意 ``chunk_index`` 参与 ID，意味着**改切分参数等于换 ID 空间**：
    ``chunk_size`` / ``overlap`` 一变，所有 chunk_id 都变，旧的索引行不会被覆盖、
    只会残留成检索不到但仍占位的"孤儿"。所以改切分参数必须走 ``reindex``
    （先按 doc_id purge 再写入）。
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
