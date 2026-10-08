"""向量检索器（Milvus + 查询侧向量化）。"""

from __future__ import annotations

from packages.common.logging import get_logger
from packages.contracts import SearchHit
from packages.embeddings import Embedder, get_embedder
from packages.embeddings.config import EmbedSettings
from packages.retrievers.base import RETRIEVER_VECTOR, FilterDict
from packages.vectorstores import MilvusSettings, MilvusStore

logger = get_logger("retrieval.vector")


class VectorRetriever:
    name = RETRIEVER_VECTOR

    def __init__(self, settings: MilvusSettings, embed_settings: EmbedSettings) -> None:
        self._embedder: Embedder = get_embedder(embed_settings)
        # 查询侧必须使用与入库时**同一模型**，否则向量空间错位、召回静默变差
        self._store = MilvusStore(settings, dim=self._embedder.dim)

    @property
    def dim(self) -> int:
        return self._embedder.dim

    @property
    def model_name(self) -> str:
        return self._embedder.model_name

    async def ensure(self) -> None:
        await self._store.ensure_collection()

    async def aclose(self) -> None:
        await self._store.aclose()

    async def retrieve(
        self, query: str, top_k: int, filters: FilterDict | None = None
    ) -> list[SearchHit]:
        vector = await self._embedder.embed_query(query)
        return await self._store.search(vector, top_k=top_k, filters=filters)

    async def count(self) -> int:
        return await self._store.count()

    async def health(self) -> tuple[bool, str]:
        return await self._store.health()
