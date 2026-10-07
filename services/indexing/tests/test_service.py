"""IndexService 编排逻辑测试（用假写入器，不依赖 Milvus / OpenSearch）。"""

from __future__ import annotations

import pytest

from packages.common.errors import ValidationError
from packages.contracts import ACL, Chunk, IndexRequest

from app import service as service_module


class FakeEmbeddingPipeline:
    dim = 8
    model_name = "fake-embed"

    def __init__(self, settings) -> None:  # noqa: ARG002
        self.embedded: list[str] = []

    async def embed_chunks(self, chunks: list[Chunk]) -> list[list[float]]:
        self.embedded = [c.text for c in chunks]
        return [[0.1] * self.dim for _ in chunks]

    async def aclose(self) -> None:
        return None


class FakeVectorWriter:
    def __init__(self, settings, dim: int) -> None:  # noqa: ARG002
        self.collection = "fake"
        self.purged: list[str] = []
        self.rows: list[Chunk] = []

    async def ensure(self) -> None:
        return None

    async def write(self, chunks: list[Chunk], vectors: list[list[float]]) -> int:
        assert len(chunks) == len(vectors)
        self.rows = chunks
        return len(chunks)

    async def delete_document(self, doc_id: str) -> int:
        self.purged.append(doc_id)
        return 1

    async def count(self) -> int:
        return len(self.rows)

    async def health(self) -> tuple[bool, str]:
        return True, "fake"


class FakeSearchWriter:
    def __init__(self, settings) -> None:  # noqa: ARG002
        self.index = "fake"
        self.purged: list[str] = []
        self.rows: list[Chunk] = []

    async def ensure(self) -> None:
        return None

    async def write(self, chunks: list[Chunk]) -> int:
        self.rows = chunks
        return len(chunks)

    async def delete_document(self, doc_id: str) -> int:
        self.purged.append(doc_id)
        return 1

    async def count(self) -> int:
        return len(self.rows)

    async def health(self) -> tuple[bool, str]:
        return True, "fake"

    async def aclose(self) -> None:
        return None


@pytest.fixture()
def service(monkeypatch):
    monkeypatch.setattr(service_module, "EmbeddingPipeline", FakeEmbeddingPipeline)
    monkeypatch.setattr(service_module, "VectorWriter", FakeVectorWriter)
    monkeypatch.setattr(service_module, "SearchWriter", FakeSearchWriter)
    from app.config import Settings

    return service_module.IndexService(Settings())


def _chunk(doc_id: str, index: int) -> Chunk:
    return Chunk(
        chunk_id=f"{doc_id}:{index}",
        doc_id=doc_id,
        text=f"第 {index} 段",
        chunk_index=index,
        char_start=index * 10,
        char_end=index * 10 + 10,
        section_path="第三章 福利",
        doc_title="员工手册",
        source="data/corpus/employee_handbook.md",
        acl=ACL(tenant_id="default", department_id="hr"),
    )


async def test_index_writes_both_stores(service) -> None:
    result = await service.index(IndexRequest(chunks=[_chunk("d_1", 0), _chunk("d_1", 1)]))
    assert result.status == "ok"
    assert result.chunks_indexed == 2
    assert result.milvus == 2 and result.opensearch == 2
    assert service._vectors.rows and service._search.rows


async def test_reindex_purges_before_write(service) -> None:
    await service.index(IndexRequest(chunks=[_chunk("d_1", 0)], reindex=True))
    assert service._vectors.purged == ["d_1"]
    assert service._search.purged == ["d_1"]


async def test_mixed_doc_ids_rejected(service) -> None:
    with pytest.raises(ValidationError):
        await service.index(IndexRequest(chunks=[_chunk("d_1", 0), _chunk("d_2", 0)]))


async def test_empty_chunks_rejected(service) -> None:
    with pytest.raises(ValidationError):
        await service.index(IndexRequest(chunks=[]))
