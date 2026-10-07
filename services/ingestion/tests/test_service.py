"""接入编排测试（用假元数据与假 sink，不依赖 Postgres / indexing）。"""

from __future__ import annotations

import pytest
from app import service as service_module

from packages.common.errors import ValidationError
from packages.contracts import ACL, Chunk, IndexResponse, IngestRequest

CONTENT = """# 员工手册

## 第三章 福利

### 3.2 入职体检

入职体检费用由员工先行垫付，转正后凭发票报销，上限五百元。
"""


class FakeMetadata:
    def __init__(self) -> None:
        self.documents: dict[str, str] = {}
        self.chunks: dict[str, list[Chunk]] = {}
        self.jobs: dict[str, dict] = {}

    async def ensure_tenant(self, tenant_id: str, name: str | None = None) -> None:
        return None

    async def create_job(self, source: str) -> str:
        self.jobs["job-1"] = {"source": source, "status": "running"}
        return "job-1"

    async def update_job(
        self, job_id: str, *, status: str, doc_id=None, chunk_count=0, message=None
    ) -> None:
        self.jobs[job_id].update({"status": status, "doc_id": doc_id, "message": message})

    async def upsert_document(self, doc, *, status: str = "pending") -> None:
        self.documents[doc.doc_id] = status

    async def replace_chunks(self, doc_id: str, chunks: list[Chunk]) -> int:
        self.chunks[doc_id] = chunks
        return len(chunks)

    async def update_document_status(self, doc_id: str, status: str, chunk_count=None) -> None:
        self.documents[doc_id] = status

    async def get_document(self, doc_id: str) -> dict | None:
        if doc_id not in self.documents:
            return None
        return {"doc_id": doc_id, "status": self.documents[doc_id]}

    async def delete_document(self, doc_id: str) -> None:
        self.documents.pop(doc_id, None)
        self.chunks.pop(doc_id, None)

    async def health(self) -> tuple[bool, str]:
        return True, "fake"


class FakeSink:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[int, bool]] = []

    async def send(self, chunks: list[Chunk], *, reindex: bool) -> IndexResponse:
        self.calls.append((len(chunks), reindex))
        if self.fail:
            raise RuntimeError("indexing down")
        return IndexResponse(
            doc_id=chunks[0].doc_id,
            chunks_indexed=len(chunks),
            milvus=len(chunks),
            opensearch=len(chunks),
        )

    async def delete_document(self, doc_id: str) -> dict[str, int]:
        self.calls.append((-1, False))
        return {"milvus": 1, "opensearch": 1}

    async def ping(self) -> tuple[bool, str]:
        return not self.fail, "fake"

    async def aclose(self) -> None:
        return None


@pytest.fixture()
def wire(monkeypatch):
    state: dict[str, object] = {}

    def factory(sink_fail: bool = False, fail_fast: bool = True):
        metadata = FakeMetadata()
        sink = FakeSink(fail=sink_fail)
        monkeypatch.setattr(service_module, "MetadataStore", lambda url: metadata)
        monkeypatch.setattr(service_module, "build_sink", lambda **kwargs: sink)
        from app.config import Settings

        service = service_module.IngestionService(
            Settings(fail_fast_on_index_error=fail_fast, chunk_size=120, chunk_overlap=20)
        )
        state.update({"metadata": metadata, "sink": sink, "service": service})
        return state

    return factory


async def test_ingest_inline_content(wire) -> None:
    state = wire()
    result = await state["service"].ingest(
        IngestRequest(
            content=CONTENT, filename="handbook.md", acl=ACL(tenant_id="t1", department_id="hr")
        )
    )
    assert result.status == "succeeded"
    assert result.documents == 1
    assert result.chunk_count >= 1
    metadata = state["metadata"]
    assert metadata.jobs["job-1"]["status"] == "succeeded"
    assert list(metadata.documents.values()) == ["indexed"]


async def test_document_status_becomes_failed_when_indexing_fails(wire) -> None:
    state = wire(sink_fail=True)
    with pytest.raises(RuntimeError):
        await state["service"].ingest(IngestRequest(content=CONTENT, filename="h.md"))
    assert list(state["metadata"].documents.values()) == ["failed"]
    assert state["metadata"].jobs["job-1"]["status"] == "failed"


async def test_ingest_requires_target(wire) -> None:
    state = wire()
    with pytest.raises(ValidationError):
        await state["service"].ingest(IngestRequest())


async def test_delete_document_clears_index_and_metadata(wire) -> None:
    state = wire()
    metadata = state["metadata"]
    metadata.documents["d_x"] = "indexed"
    metadata.chunks["d_x"] = []

    result = await state["service"].delete_document("d_x")
    assert result["existed"] is True
    assert result["deleted"] == {"milvus": 1, "opensearch": 1}
    assert "d_x" not in metadata.documents
    assert "d_x" not in metadata.chunks


async def test_timings_are_reported(wire) -> None:
    state = wire()
    result = await state["service"].ingest(IngestRequest(content=CONTENT, filename="h.md"))
    assert {"parse", "chunk", "metadata", "index"} <= set(result.timings_ms)
