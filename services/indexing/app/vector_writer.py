"""向量写入器（Milvus）。"""

from __future__ import annotations

from packages.common.logging import get_logger
from packages.contracts import Chunk
from packages.vectorstores import MilvusSettings, MilvusStore

logger = get_logger("indexing.vector_writer")


class VectorWriter:
    def __init__(self, settings: MilvusSettings, dim: int) -> None:
        self._store = MilvusStore(settings, dim=dim)

    @property
    def collection(self) -> str:
        return self._store.collection

    async def ensure(self) -> None:
        await self._store.ensure_collection()

    async def write(self, chunks: list[Chunk], vectors: list[list[float]]) -> int:
        return await self._store.upsert(chunks, vectors)

    async def delete_document(self, doc_id: str) -> int:
        return await self._store.delete_by_doc(doc_id)

    async def count(self) -> int:
        return await self._store.count()

    async def health(self) -> tuple[bool, str]:
        return await self._store.health()
