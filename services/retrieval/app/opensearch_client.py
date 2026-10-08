"""BM25 检索器（OpenSearch）。"""

from __future__ import annotations

from packages.common.logging import get_logger
from packages.contracts import SearchHit
from packages.retrievers.base import RETRIEVER_BM25, FilterDict
from packages.search import OpenSearchSettings, OpenSearchStore

logger = get_logger("retrieval.opensearch")


class BM25Retriever:
    """BM25 检索器：委托 ``OpenSearchStore`` 实现 ``Retriever`` 协议（关键词一路）。"""

    name = RETRIEVER_BM25

    def __init__(self, settings: OpenSearchSettings) -> None:
        self._store = OpenSearchStore(settings)

    async def ensure(self) -> None:
        await self._store.ensure_index()

    async def retrieve(
        self, query: str, top_k: int, filters: FilterDict | None = None
    ) -> list[SearchHit]:
        return await self._store.search(query, top_k=top_k, filters=filters)

    async def count(self) -> int:
        return await self._store.count()

    async def health(self) -> tuple[bool, str]:
        return await self._store.health()

    async def aclose(self) -> None:
        await self._store.aclose()
