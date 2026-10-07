"""Milvus 适配：建表（含 ACL 标量字段）、幂等写入、向量检索、按文档删除。

关键设计：
    * **ACL 以四个标量字段落在 schema 里**（tenant_id / department_id /
      visibility / owner），并已建 INVERTED 索引、过滤表达式已实现并生效
      （见 ``SCALAR_INDEX_FIELDS`` 与 :meth:`MilvusStore._compile_expr`）。
      早期"P2 再补过滤"的计划已完成——新增可见性维度时改 schema + 表达式编译两处；
    * 主键用 ``chunk_id``（VARCHAR）而非自增 INT64 —— 重跑索引天然幂等，不产生重复；
    * 写入后 ``flush``，否则「刚写就查不到」会被误判为向量没写进去；
    * 过滤下沉为 ``expr``，绝不在应用层裁剪 top_k。
"""

from __future__ import annotations

import asyncio
from typing import Any

from pymilvus import DataType, MilvusClient

from packages.common.errors import DependencyUnavailable
from packages.common.logging import get_logger
from packages.contracts import Chunk, SearchHit
from packages.retrievers.base import FilterDict
from packages.vectorstores.config import MilvusSettings

logger = get_logger("vectorstores.milvus")

OUTPUT_FIELDS = [
    "chunk_id",
    "doc_id",
    "doc_title",
    "source",
    "text",
    "chunk_index",
    "section_path",
    "char_start",
    "char_end",
    "tenant_id",
    "department_id",
    "visibility",
    "owner",
]

# 需要建 INVERTED 索引的标量字段（过滤走索引而不是暴力扫描）
SCALAR_INDEX_FIELDS = ["doc_id", "tenant_id", "department_id", "visibility", "owner"]


def _quote(value: str) -> str:
    """Milvus expr 字符串字面量转义。"""
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_collection_schema(client: MilvusClient, dim: int, text_max_length: int) -> Any:
    schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field("chunk_id", DataType.VARCHAR, max_length=128, is_primary=True)
    schema.add_field("vector", DataType.FLOAT_VECTOR, dim=dim)
    schema.add_field("doc_id", DataType.VARCHAR, max_length=64)
    schema.add_field("doc_title", DataType.VARCHAR, max_length=512)
    schema.add_field("source", DataType.VARCHAR, max_length=1024)
    schema.add_field("text", DataType.VARCHAR, max_length=text_max_length)
    schema.add_field("chunk_index", DataType.INT32)
    schema.add_field("section_path", DataType.VARCHAR, max_length=512)
    schema.add_field("char_start", DataType.INT32)
    schema.add_field("char_end", DataType.INT32)
    # ---- ACL 标量（P2 权限过滤直接复用，无需改 schema）----
    schema.add_field("tenant_id", DataType.VARCHAR, max_length=64)
    schema.add_field("department_id", DataType.VARCHAR, max_length=64)
    schema.add_field("visibility", DataType.VARCHAR, max_length=32)
    schema.add_field("owner", DataType.VARCHAR, max_length=128)
    return schema


def build_index_params(client: MilvusClient, settings: MilvusSettings) -> Any:
    params = client.prepare_index_params()
    params.add_index(
        field_name="vector",
        index_type="HNSW",
        metric_type="COSINE",
        params={
            "M": settings.milvus_hnsw_m,
            "efConstruction": settings.milvus_hnsw_ef_construction,
        },
    )
    for field in SCALAR_INDEX_FIELDS:
        params.add_index(field_name=field, index_type="INVERTED")
    return params


class MilvusStore:
    name = "vector"

    def __init__(self, settings: MilvusSettings, dim: int) -> None:
        self._settings = settings
        self.collection = settings.milvus_collection
        self.dim = dim
        try:
            self._client = MilvusClient(uri=settings.milvus_uri, timeout=settings.milvus_timeout)
        except Exception as exc:  # noqa: BLE001
            raise DependencyUnavailable("Milvus", f"连接失败 {settings.milvus_uri}: {exc}") from exc

    # ---------------- 索引管理 ----------------
    def _ensure_collection_sync(self) -> None:
        if self._client.has_collection(self.collection):
            return
        schema = build_collection_schema(
            self._client, self.dim, self._settings.milvus_text_max_length
        )
        index_params = build_index_params(self._client, self._settings)
        self._client.create_collection(
            collection_name=self.collection, schema=schema, index_params=index_params
        )
        self._client.load_collection(self.collection)
        logger.info("已创建 Milvus collection %s (dim=%s)", self.collection, self.dim)

    def _has_collection_sync(self) -> bool:
        try:
            return bool(self._client.has_collection(self.collection))
        except Exception:  # noqa: BLE001
            return False

    async def ensure_collection(self) -> None:
        """幂等建表。并发启动时 retrieval 与 indexing 会同时建同一个 collection，
        因此创建失败后需再确认一次是否已被对端建好。"""
        try:
            await asyncio.to_thread(self._ensure_collection_sync)
        except Exception as exc:  # noqa: BLE001
            if await asyncio.to_thread(self._has_collection_sync):
                logger.info("Milvus collection %s 已由其他实例创建", self.collection)
                return
            raise DependencyUnavailable("Milvus", f"collection 初始化失败: {exc}") from exc

    async def health(self) -> tuple[bool, str]:
        try:
            collections = await asyncio.to_thread(self._client.list_collections)
            return True, f"uri={self._settings.milvus_uri} collections={len(collections)}"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def count(self) -> int:
        try:
            rows = await asyncio.to_thread(
                self._client.query,
                collection_name=self.collection,
                filter="",
                output_fields=["count(*)"],
            )
            if rows:
                return int(rows[0].get("count(*)", 0))
            return 0
        except Exception:  # noqa: BLE001
            return 0

    # ---------------- 写入 ----------------
    def _row(self, chunk: Chunk, vector: list[float]) -> dict[str, Any]:
        return {
            "chunk_id": chunk.chunk_id,
            "vector": vector,
            "doc_id": chunk.doc_id,
            "doc_title": chunk.doc_title,
            "source": chunk.source,
            # 按字节截断，避免中文超 max_length 直接插入报错。
            # ⚠️ 口径提示：VARCHAR 的 max_length 是**字节**上限，所以 text 用字节截断；
            # 而下面 section_path/owner 是按**字符**截断的（[:512] / [:128]）。
            # 混用不会立刻报错，只会在字段真超长时被 Milvus 拒写。要严格对齐，
            # 需把这两处也改成字节截断。
            # 另一处副作用：text 被截断后 char_start/char_end 仍指向**原文**，
            # 所以 Milvus 里的 text 与"按偏移回查原文"的结果可能不一致——
            # 引用高亮请以元数据里的原文为准，不要拿向量库里的 text 做高亮。
            "text": chunk.text.encode("utf-8")[: self._settings.milvus_text_max_length].decode(
                "utf-8", errors="ignore"
            ),
            "chunk_index": chunk.chunk_index,
            "section_path": chunk.section_path[:512],
            "char_start": chunk.char_start,
            "char_end": chunk.char_end,
            "tenant_id": chunk.acl.tenant_id,
            "department_id": chunk.acl.department_id,
            "visibility": chunk.acl.visibility.value,
            "owner": (chunk.acl.owner or "")[:128],
        }

    def _upsert_sync(self, rows: list[dict[str, Any]]) -> int:
        result = self._client.upsert(collection_name=self.collection, data=rows)
        # 立即 flush，保证随后检索可见
        self._client.flush(self.collection)
        return int(result.get("upsert_count", len(rows)))

    async def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> int:
        if not chunks:
            return 0
        if len(chunks) != len(vectors):
            raise DependencyUnavailable(
                "Milvus", f"chunk 数与向量数不一致: {len(chunks)} != {len(vectors)}"
            )
        rows = [self._row(c, v) for c, v in zip(chunks, vectors, strict=True)]
        try:
            return await asyncio.to_thread(self._upsert_sync, rows)
        except Exception as exc:  # noqa: BLE001
            raise DependencyUnavailable("Milvus", f"写入失败: {exc}") from exc

    async def delete_by_doc(self, doc_id: str) -> int:
        try:
            await asyncio.to_thread(
                self._client.delete,
                collection_name=self.collection,
                filter=f"doc_id == {_quote(doc_id)}",
            )
            await asyncio.to_thread(self._client.flush, self.collection)
            return 1
        except Exception as exc:  # noqa: BLE001
            raise DependencyUnavailable("Milvus", f"删除失败: {exc}") from exc

    # ---------------- 检索 ----------------
    @staticmethod
    def _compile_expr(filters: FilterDict | None) -> str:
        """把过滤契约编译为 Milvus expr（检索侧 ACL 的执行点之一）。

        与 :meth:`packages.search.opensearch.OpenSearchStore._compile_filter` 必须
        1:1 对应，语义源是 ``packages/retrievers/filters.py`` 的模块 docstring：
        clause 之间 OR、clause 内部 AND、must 与所有 clause 之间 AND。

        空契约返回空字符串，而**空 expr 在 Milvus 里等于不过滤**（match-all）。
        调用方不要把"空 expr"当成"没有查询条件所以安全"——
        它和"没有权限约束"是同一件事，见 filters.compile_filters 的告警。
        """
        if not filters:
            return ""
        parts: list[str] = []
        for field, value in (filters.get("must") or {}).items():
            parts.append(f"{field} == {_quote(value)}")

        visibility_clauses = filters.get("visibility_clauses") or []
        if visibility_clauses:
            ors: list[str] = []
            for clause in visibility_clauses:
                ands = [f"{k} == {_quote(v)}" for k, v in clause.items()]
                ors.append("(" + " and ".join(ands) + ")")
            parts.append("(" + " or ".join(ors) + ")")

        doc_ids = filters.get("doc_ids") or []
        if doc_ids:
            joined = ", ".join(_quote(d) for d in doc_ids)
            parts.append(f"doc_id in [{joined}]")

        return " and ".join(parts)

    def _search_sync(self, vector: list[float], top_k: int, expr: str) -> list[dict[str, Any]]:
        return self._client.search(
            collection_name=self.collection,
            data=[vector],
            limit=top_k,
            filter=expr or "",
            output_fields=OUTPUT_FIELDS,
            search_params={
                "metric_type": "COSINE",
                "params": {"ef": self._settings.milvus_search_ef},
            },
        )

    async def search(
        self,
        vector: list[float],
        *,
        top_k: int = 20,
        filters: FilterDict | None = None,
    ) -> list[SearchHit]:
        expr = self._compile_expr(filters)
        try:
            raw = await asyncio.to_thread(self._search_sync, vector, top_k, expr)
        except Exception as exc:  # noqa: BLE001
            raise DependencyUnavailable("Milvus", f"检索失败: {exc}") from exc

        hits: list[SearchHit] = []
        for group in raw or []:
            for item in group:
                entity = item.get("entity", item) or {}
                hits.append(
                    SearchHit(
                        chunk_id=entity.get("chunk_id", ""),
                        doc_id=entity.get("doc_id", ""),
                        doc_title=entity.get("doc_title", ""),
                        source=entity.get("source", ""),
                        text=entity.get("text", ""),
                        chunk_index=int(entity.get("chunk_index", 0) or 0),
                        section_path=entity.get("section_path", ""),
                        char_start=int(entity.get("char_start", 0) or 0),
                        char_end=int(entity.get("char_end", 0) or 0),
                        # COSINE 距离即相似度，越大越相近
                        score=float(item.get("distance") or 0.0),
                        retriever=self.name,
                    )
                )
        return hits
