"""稀疏检索：Milvus 内置 BM25（中文分词走 jieba）。

价值：命中错误码、版本号、专有名词等精确/近似精确查询。
"""

from app.schemas.retrieval import Chunk, RetrievalFilters


def search_sparse(
    query: str,
    top_n: int,
    filters: RetrievalFilters | None = None,
) -> list[Chunk]:
    """稀疏（BM25）通道召回。"""
    # TODO(后续)：pymilvus collection.search(sparse, ...)，配置 k1/b
    raise NotImplementedError("骨架占位：search_sparse 将在后续编码阶段实现")
