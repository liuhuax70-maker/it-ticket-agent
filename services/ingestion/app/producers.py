"""分块产出通道（ChunkSink）。

最小闭环默认 ``HttpChunkSink``：**同步直连** indexing，一次 ingest 结束即完成入库，
元数据与索引状态不会出现「写了一半」的窗口。

``KafkaChunkSink`` 是异步通道的占位实现：接口一致，切过去时 service 层不用改。
"""

from __future__ import annotations

from typing import Protocol

from packages.common.http import ServiceClient
from packages.common.kafka import KafkaPublisher
from packages.common.logging import get_logger
from packages.contracts import Chunk, IndexRequest, IndexResponse

logger = get_logger("ingestion.producers")


class ChunkSink(Protocol):
    async def send(self, chunks: list[Chunk], *, reindex: bool) -> IndexResponse: ...

    async def delete_document(self, doc_id: str) -> dict[str, int]: ...

    async def ping(self) -> tuple[bool, str]: ...

    async def aclose(self) -> None: ...


class HttpChunkSink:
    """同步直连 indexing。"""

    def __init__(self, base_url: str, timeout: float = 300.0) -> None:
        self._client = ServiceClient(base_url, name="indexing", timeout=timeout)

    async def send(self, chunks: list[Chunk], *, reindex: bool) -> IndexResponse:
        return await self._client.post(
            "/index",
            IndexRequest(chunks=chunks, reindex=reindex),
            response_model=IndexResponse,
        )

    async def delete_document(self, doc_id: str) -> dict[str, int]:
        resp = await self._client.post(f"/documents/{doc_id}/delete")
        deleted = (resp or {}).get("deleted", {}) or {}
        return {k: int(v) for k, v in deleted.items()}

    async def ping(self) -> tuple[bool, str]:
        ok = await self._client.ping("/health")
        return ok, f"http {self._client.base_url}" if ok else f"unreachable {self._client.base_url}"

    async def aclose(self) -> None:
        await self._client.aclose()


class KafkaChunkSink:
    """异步通道（占位）。

    返回的 ``IndexResponse`` 只表示**投递成功**，此时索引尚未写入，
    因此 milvus / opensearch 计数如实为 0，请以 indexing 侧日志为准。
    """

    def __init__(self, bootstrap: str, topic: str) -> None:
        self._publisher = KafkaPublisher(bootstrap, topic)
        self.topic = topic

    async def start(self) -> None:
        await self._publisher.start()

    async def send(self, chunks: list[Chunk], *, reindex: bool) -> IndexResponse:
        payload = {
            "reindex": reindex,
            "chunks": [c.model_dump(mode="json") for c in chunks],
        }
        # key=doc_id 是**正确性前提**，不是分区均衡的优化：
        # 同一文档的所有事件进同一分区，从而保证 purge（reindex）→ write 的先后顺序。
        # 若改成随机 key 或按 chunk_id 分区，同一文档的删除与写入可能乱序到达消费端，
        # 结果是"先写后删"——文档刚建好就被删掉，且没有任何报错。
        await self._publisher.publish(payload, key=chunks[0].doc_id if chunks else None)
        logger.info("已投递 chunk-events: doc_id=%s chunks=%s", chunks[0].doc_id if chunks else "-", len(chunks))
        return IndexResponse(
            doc_id=chunks[0].doc_id if chunks else "",
            chunks_indexed=len(chunks),
            status="ok",
        )

    async def delete_document(self, doc_id: str) -> dict[str, int]:
        """投递删除事件。异步通道下索引删除是最终一致的，此处无法给出条数。"""
        await self._publisher.publish({"op": "delete", "doc_id": doc_id}, key=doc_id)
        logger.info("已投递删除事件 doc_id=%s", doc_id)
        return {"milvus": 0, "opensearch": 0}

    async def ping(self) -> tuple[bool, str]:
        # 生产者未启动时不报错：Kafka 是异步通道，未启用时不应拉低健康分
        ready = self._publisher._producer is not None  # noqa: SLF001
        return ready, f"kafka {self.topic} started={ready}"

    async def aclose(self) -> None:
        await self._publisher.stop()


def build_sink(*, use_kafka: bool, indexing_url: str, timeout: float, bootstrap: str, topic: str) -> ChunkSink:
    if use_kafka:
        logger.info("ChunkSink=Kafka topic=%s", topic)
        return KafkaChunkSink(bootstrap, topic)
    logger.info("ChunkSink=HTTP(同步直连) url=%s", indexing_url)
    return HttpChunkSink(indexing_url, timeout=timeout)
