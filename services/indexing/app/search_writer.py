"""全文索引写入器（OpenSearch / BM25）。"""

from __future__ import annotations

from packages.common.logging import get_logger
from packages.contracts import Chunk
from packages.search import OpenSearchSettings, OpenSearchStore

logger = get_logger("indexing.search_writer")


class SearchWriter:
    """全文索引写入器：委托 ``OpenSearchStore`` 实现 ensure/write/delete/count/health。"""

    def __init__(self, settings: OpenSearchSettings) -> None:
        self._store = OpenSearchStore(settings)

    @property
    def index(self) -> str:
        return self._store.index

    async def ensure(self) -> None:
        await self._store.ensure_index()

    async def write(self, chunks: list[Chunk]) -> int:
        return await self._store.bulk_index(chunks)

    async def delete_document(self, doc_id: str) -> int:
        return await self._store.delete_by_doc(doc_id)

    async def count(self) -> int:
        return await self._store.count()

    async def health(self) -> tuple[bool, str]:
        return await self._store.health()

    async def aclose(self) -> None:
        await self._store.aclose()
