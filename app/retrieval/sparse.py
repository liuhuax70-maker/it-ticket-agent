"""稀疏检索：Milvus 内置 BM25。

价值：精确命中错误码、版本号、专有名词等查询（稠密向量容易语义漂移）。

用法要点：全文检索时**直接把 query 文本交给 Milvus**，
Milvus 用 `content` 字段的分析器（jieba）切词后在 `sparse` 字段上做 BM25 打分，
因此这里不需要（也不能）自己构造稀疏向量。
"""

from app.core.config import get_settings
from app.core.logging import get_logger
from app.retrieval.milvus_store import (
    FIELD_SPARSE,
    OUTPUT_FIELDS,
    build_filter_expr,
    get_client,
    to_chunks,
)
from app.schemas.retrieval import Chunk, RetrievalFilters

logger = get_logger(__name__)


def search_sparse(
    query: str,
    top_n: int,
    filters: RetrievalFilters | None = None,
) -> list[Chunk]:
    """稀疏（BM25）通道召回。"""
    settings = get_settings()

    results = get_client().search(
        collection_name=settings.milvus_collection,
        data=[query],  # 全文检索：传原始文本
        anns_field=FIELD_SPARSE,
        limit=top_n,
        output_fields=OUTPUT_FIELDS,
        search_params={"metric_type": "BM25", "params": {}},
        filter=build_filter_expr(filters),
    )
    return to_chunks(results, score_field="sparse_score")
