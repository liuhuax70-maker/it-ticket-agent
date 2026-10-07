"""向量化流水线：批量 + 可选 Redis 缓存。

缓存命中判断按「文本 + 模型」做，与向量库写入是两件事：
缓存只省算力，是否重建索引由 ingestion 的 reindex 决定。
"""

from __future__ import annotations

from packages.common.logging import get_logger
from packages.contracts import Chunk
from packages.embeddings import Embedder, get_embedder
from packages.embeddings.cache import EmbeddingCache
from packages.embeddings.config import EmbedSettings

logger = get_logger("indexing.embedder")


class EmbeddingPipeline:
    def __init__(self, settings: EmbedSettings) -> None:
        self._settings = settings
        self._embedder: Embedder = get_embedder(settings)
        self._cache: EmbeddingCache | None = None
        if settings.embed_cache_enabled:
            self._cache = EmbeddingCache(settings.redis_url, settings.cache_ttl_seconds)
            logger.info("embedding 缓存已启用")

    @property
    def dim(self) -> int:
        return self._embedder.dim

    @property
    def model_name(self) -> str:
        return self._embedder.model_name

    async def embed_chunks(self, chunks: list[Chunk]) -> list[list[float]]:
        if not chunks:
            return []
        texts = [c.text for c in chunks]

        if self._cache is None:
            return await self._embedder.embed_documents(texts)

        cached = await self._cache.get_many(self.model_name, "document", texts)
        missing_idx = [i for i, vec in enumerate(cached) if vec is None]
        if not missing_idx:
            logger.info("embedding 全部命中缓存: %s 条", len(texts))
            return [vec for vec in cached if vec is not None]

        computed = await self._embedder.embed_documents([texts[i] for i in missing_idx])
        for i, vec in zip(missing_idx, computed, strict=True):
            cached[i] = vec
        # ⚠️ 写回缓存的 value 列表必须与 texts **严格同长同序**：
        # 缓存按文本哈希存键，调用方按位置对齐，任何"过滤掉 None"之类的写法都会让
        # 某个 chunk 的文本配到另一个 chunk 的向量上——召回结果错配且不报任何错。
        await self._cache.set_many(
            self.model_name, "document", texts, [c for c in cached if c is not None]
        )
        logger.info("embedding 计算 %s 条，命中缓存 %s 条", len(missing_idx), len(texts) - len(missing_idx))
        return [vec for vec in cached if vec is not None]

    async def aclose(self) -> None:
        if self._cache is not None:
            await self._cache.aclose()
