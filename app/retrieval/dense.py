"""稠密检索：qwen3-embedding（Ollama）+ Milvus ANN。

向量化走 Ollama 的 `/api/embed`，检索走 Milvus 的 HNSW（度量 COSINE）。
"""

import httpx

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.retrieval.milvus_store import (
    FIELD_DENSE,
    OUTPUT_FIELDS,
    build_filter_expr,
    get_client,
    to_chunks,
)
from app.schemas.retrieval import Chunk, RetrievalFilters

logger = get_logger(__name__)

#: 单次 embedding 请求超时（首个请求含模型加载，给足时间）
EMBED_TIMEOUT = 120.0
#: 批量大小，避免单次请求体过大
EMBED_BATCH_SIZE = 16


def embed(
    texts: list[str],
    *,
    model: str | None = None,
    timeout: float = EMBED_TIMEOUT,
) -> list[list[float]]:
    """调用 Ollama 生成向量（批量）。

    :raises AppError: Ollama 不可用或返回异常时（错误码 2002）。
    """
    if not texts:
        return []

    settings = get_settings()
    url = f"{settings.ollama_base_url.rstrip('/')}/api/embed"
    payload = {"model": model or settings.embedding_model, "input": texts}

    try:
        response = httpx.post(url, json=payload, timeout=timeout)
        response.raise_for_status()
        embeddings = response.json()["embeddings"]
    except httpx.HTTPError as exc:
        raise AppError(
            ErrorCode.GENERATION_SERVICE_UNAVAILABLE,
            f"Embedding 服务调用失败: {exc}",
        ) from exc
    except (KeyError, ValueError) as exc:
        raise AppError(
            ErrorCode.GENERATION_SERVICE_UNAVAILABLE,
            f"Embedding 响应格式异常: {exc}",
        ) from exc

    if len(embeddings) != len(texts):
        raise AppError(
            ErrorCode.GENERATION_SERVICE_UNAVAILABLE,
            f"Embedding 数量不匹配: 期望 {len(texts)}，实际 {len(embeddings)}",
        )
    return embeddings


def embed_batched(
    texts: list[str],
    *,
    batch_size: int = EMBED_BATCH_SIZE,
    model: str | None = None,
) -> list[list[float]]:
    """分批向量化，避免单次请求体过大。"""
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        vectors.extend(embed(batch, model=model))
    return vectors


def search_dense(
    query: str,
    top_n: int,
    filters: RetrievalFilters | None = None,
) -> list[Chunk]:
    """稠密通道召回（HNSW + COSINE）。"""
    settings = get_settings()
    query_vector = embed([query])[0]

    results = get_client().search(
        collection_name=settings.milvus_collection,
        data=[query_vector],
        anns_field=FIELD_DENSE,
        limit=top_n,
        output_fields=OUTPUT_FIELDS,
        search_params={"metric_type": "COSINE", "params": {"ef": 64}},
        filter=build_filter_expr(filters),
    )
    return to_chunks(results, score_field="dense_score")
