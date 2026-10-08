"""入库编排：向量化 -> 写 Milvus -> 写 OpenSearch。

幂等性说明：
    * Milvus 主键 = chunk_id，OpenSearch _id = chunk_id，重复写入是覆盖而非新增；
    * 因此常规重建不必先删——但文档**变短**时会残留旧 chunk，
      所以 ``reindex=True`` 时先按 doc_id 清理两处索引。
"""

from __future__ import annotations

import time
from typing import Literal

from app.config import Settings
from app.embedder import EmbeddingPipeline
from app.search_writer import SearchWriter
from app.vector_writer import VectorWriter
from packages.common.errors import ValidationError
from packages.common.logging import get_logger
from packages.contracts import IndexRequest, IndexResponse
from packages.search.config import OpenSearchSettings
from packages.vectorstores.config import MilvusSettings

logger = get_logger("indexing.service")


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


class IndexService:
    """入库编排：向量化 → 写 Milvus → 写 OpenSearch（幂等）。

    ``reindex=True`` 时先 purge 两处索引再写（purge 必须排在 embed 之前，否则 chunk_id 错位
    导致新旧 chunk 共存）；以 embedder 实际维度建表，避免 EMBED_DIM 与实际模型不一致。
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._embedder = EmbeddingPipeline(settings)
        # 以 embedder 实际维度建表，避免 EMBED_DIM 配置与实际模型不一致导致写入报错
        milvus_settings: MilvusSettings = settings
        search_settings: OpenSearchSettings = settings
        self._vectors = VectorWriter(milvus_settings, dim=self._embedder.dim)
        self._search = SearchWriter(search_settings)

    @property
    def dim(self) -> int:
        return self._embedder.dim

    async def startup(self) -> None:
        await self._vectors.ensure()
        await self._search.ensure()
        logger.info(
            "indexing 就绪: embed=%s dim=%s collection=%s index=%s",
            self._embedder.model_name,
            self.dim,
            self._vectors.collection,
            self._search.index,
        )

    async def index(self, req: IndexRequest) -> IndexResponse:
        if not req.chunks:
            raise ValidationError("chunks 为空，无可写入内容")

        doc_ids = {c.doc_id for c in req.chunks}
        if len(doc_ids) != 1:
            raise ValidationError(f"一次 /index 只允许处理单个文档，收到 {len(doc_ids)} 个 doc_id")
        doc_id = doc_ids.pop()

        timings: dict[str, float] = {}

        if req.reindex:
            # purge 必须排在 embed **之前**：否则改切分参数后 chunk_id 整体错位，
            # 新旧 chunk 会同时留在索引里（见 packages.common.ids.stable_chunk_id）。
            # 代价是有一个**失败窗口**：purge 成功而后续 embed/写入失败时，
            # 该文档会立即变成完全检索不到，且没有回滚（无临时索引、无补偿）。
            # 恢复方式是重跑一次 reindex——这也是 reindex 必须幂等的原因。
            started = time.perf_counter()
            await self._vectors.delete_document(doc_id)
            await self._search.delete_document(doc_id)
            timings["purge"] = _ms(started)

        started = time.perf_counter()
        vectors = await self._embedder.embed_chunks(req.chunks)
        timings["embed"] = _ms(started)

        started = time.perf_counter()
        milvus_n = await self._vectors.write(req.chunks, vectors)
        timings["milvus"] = _ms(started)

        started = time.perf_counter()
        opensearch_n = await self._search.write(req.chunks)
        timings["opensearch"] = _ms(started)

        status: Literal["ok", "partial", "failed"] = (
            "ok" if milvus_n and opensearch_n else "partial"
        )
        logger.info(
            "入库完成 doc_id=%s chunks=%s milvus=%s opensearch=%s",
            doc_id,
            len(req.chunks),
            milvus_n,
            opensearch_n,
        )
        return IndexResponse(
            doc_id=doc_id,
            chunks_indexed=len(req.chunks),
            milvus=milvus_n,
            opensearch=opensearch_n,
            status=status,
            timings_ms=timings,
        )

    async def delete_document(self, doc_id: str) -> dict[str, int]:
        milvus_n = await self._vectors.delete_document(doc_id)
        opensearch_n = await self._search.delete_document(doc_id)
        logger.info(
            "删除文档索引 doc_id=%s milvus=%s opensearch=%s", doc_id, milvus_n, opensearch_n
        )
        return {"milvus": milvus_n, "opensearch": opensearch_n}

    async def health(self) -> dict[str, str]:
        ok_v, msg_v = await self._vectors.health()
        ok_s, msg_s = await self._search.health()
        return {
            "status": "ok" if (ok_v and ok_s) else "degraded",
            "milvus": msg_v,
            "opensearch": msg_s,
            "embed_model": self._embedder.model_name,
            "embed_dim": str(self.dim),
        }

    async def aclose(self) -> None:
        await self._embedder.aclose()
        await self._search.aclose()
