"""稠密检索：qwen3-embedding + Milvus ANN。"""

from app.schemas.retrieval import Chunk, RetrievalFilters


def embed(texts: list[str]) -> list[list[float]]:
    """调用 qwen3-embedding 生成向量（批量）。"""
    # TODO(后续)：通过 Ollama/Embedding 服务生成向量
    raise NotImplementedError("骨架占位：embed 将在后续编码阶段实现")


def search_dense(
    query: str,
    top_n: int,
    filters: RetrievalFilters | None = None,
) -> list[Chunk]:
    """稠密通道召回（度量 COSINE）。"""
    # TODO(后续)：pymilvus collection.search(dense, ...)
    raise NotImplementedError("骨架占位：search_dense 将在后续编码阶段实现")
