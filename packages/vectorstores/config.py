"""Milvus 配置。"""

from __future__ import annotations

from packages.common.settings import BaseAppSettings


class MilvusSettings(BaseAppSettings):
    """Milvus（向量检索）配置。

    ⚠️ ``milvus_text_max_length`` 按**字节**计（中文 3 字节/字），务必留足余量；
    HNSW 的 ``m`` / ``ef_construction`` / ``search_ef`` 决定召回率与内存占用，
    重建 collection 前改这些参数不会自动生效。
    """

    service_name: str = "indexing"

    milvus_uri: str = "http://localhost:19530"
    milvus_collection: str = "chunks"
    milvus_timeout: float = 10.0
    # HNSW 参数：M 越大召回越高、内存越大
    milvus_hnsw_m: int = 16
    milvus_hnsw_ef_construction: int = 200
    milvus_search_ef: int = 64
    # text 字段 max_length 按**字节**计（中文 3 字节/字），务必留足余量
    milvus_text_max_length: int = 8192
