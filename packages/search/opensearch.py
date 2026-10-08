"""OpenSearch 适配：索引模板、批量写入、BM25 检索、按文档删除。

使用 ``AsyncOpenSearch`` 避免把同步客户端塞进线程池。
过滤（ACL）在 ``_compile_filter`` 内下沉为 OpenSearch ``bool.filter``。
"""

from __future__ import annotations

from typing import Any

from opensearchpy import AsyncOpenSearch
from opensearchpy.helpers import async_bulk

from packages.common.constants import LIFECYCLE_ACTIVE
from packages.common.errors import DependencyUnavailable
from packages.common.logging import get_logger
from packages.contracts import Chunk, SearchHit
from packages.retrievers.base import FilterDict
from packages.search.config import OpenSearchSettings

logger = get_logger("search.opensearch")

SOURCE_FIELDS = [
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


def build_index_body(analyzer: str) -> dict[str, Any]:
    """索引模板。text 与 section_path 走全文分析，ACL 字段一律 keyword。"""
    return {
        "settings": {
            "number_of_shards": 1,
            "number_of_replicas": 0,
            "refresh_interval": "1s",
        },
        "mappings": {
            "properties": {
                "text": {"type": "text", "analyzer": analyzer},
                "chunk_id": {"type": "keyword"},
                "doc_id": {"type": "keyword"},
                "doc_title": {
                    "type": "text",
                    "analyzer": analyzer,
                    "fields": {"keyword": {"type": "keyword"}},
                },
                "source": {"type": "keyword"},
                "chunk_index": {"type": "integer"},
                "section_path": {"type": "text", "analyzer": analyzer},
                "char_start": {"type": "integer"},
                "char_end": {"type": "integer"},
                "tenant_id": {"type": "keyword"},
                "department_id": {"type": "keyword"},
                "visibility": {"type": "keyword"},
                "owner": {"type": "keyword"},
                # 生命周期（失效管理）：已废止文档不参与检索。
                # 显式声明为 keyword：依赖 dynamic mapping 也能用，但类型一旦被自动推断成
                # text 就会分词，term 过滤会匹配不上——那是静默失效。
                "lifecycle": {"type": "keyword"},
            }
        },
    }


class OpenSearchStore:
    """BM25 全文检索存储：索引管理、幂等写入与带 ACL 过滤的检索。

    与 ``MilvusStore`` 并列实现 ``Retriever`` 协议，两者融合后构成混合检索的
    关键词一路；过滤语义必须与 ``MilvusStore._compile_expr`` 1:1 一致。
    """

    name = "bm25"

    def __init__(self, settings: OpenSearchSettings) -> None:
        self._settings = settings
        self.index = settings.opensearch_index
        http_auth = (
            (settings.opensearch_username, settings.opensearch_password)
            if settings.opensearch_username
            else None
        )
        self._client = AsyncOpenSearch(
            hosts=[settings.opensearch_url],
            http_auth=http_auth,
            use_ssl=settings.opensearch_url.startswith("https"),
            # ⚠️ 关闭证书校验：本地/自签证书集群能直连，代价是 https 链路完全不做
            # 证书验证（可被中间人）。生产集群必须改成可配置项（CA 路径 + 校验开关），
            # 不要沿用这里的默认值。
            verify_certs=False,
            timeout=settings.opensearch_timeout,
            max_retries=2,
            retry_on_timeout=True,
        )

    async def aclose(self) -> None:
        """关闭 OpenSearch 异步客户端连接。"""
        await self._client.close()

    # ---------------- 索引管理 ----------------
    async def _index_exists(self) -> bool:
        """索引是否存在。

        ⚠️ 异常一律返回 ``False``（把"鉴权失败 / 超时"当成"索引不存在"）：
        好处是 ensure_index 会继续尝试 create，从而把真实错误抛出来；
        代价是调用方**无法区分**"索引真的不存在"与"探测失败"。
        要区分需返回三态或抛专用异常。
        """
        try:
            return bool(await self._client.indices.exists(index=self.index))
        except Exception:  # noqa: BLE001
            return False

    async def ensure_index(self) -> None:
        """幂等建索引。

        ``exists`` 与 ``create`` 之间存在竞态：多个服务（retrieval 与 indexing）
        同时启动时会并发建同一个索引，后到者会收到 resource_already_exists。
        因此创建失败后要**再查一次**，只要索引已存在就算成功。
        """
        try:
            if not await self._index_exists():
                await self._client.indices.create(
                    index=self.index, body=build_index_body(self._settings.opensearch_analyzer)
                )
                logger.info("已创建 OpenSearch 索引 %s", self.index)
            else:
                # 已存在时也要**补齐 mapping**：新增字段否则会走 dynamic mapping，
                # 而它把字符串推断成 text（分词），term 过滤匹配不上——
                # 那是静默失效（写了字段、查不到），比报错难排查得多。
                # put_mapping 幂等：补缺失字段可以，改已有字段类型会被拒绝。
                await self._client.indices.put_mapping(
                    index=self.index,
                    body=build_index_body(self._settings.opensearch_analyzer)["mappings"],
                )
        except Exception as exc:  # noqa: BLE001
            if await self._index_exists():
                # 索引在、mapping 没补齐：功能可用但过滤可能退化，必须留下痕迹。
                # 不直接抛：启动失败的影响面比"过滤字段缺失"更大，且这里多为类型冲突。
                logger.warning(
                    "OpenSearch 索引 %s 的 mapping 未能补齐（%s）："
                    "新增字段的过滤可能失效，请检查字段类型冲突",
                    self.index,
                    exc,
                )
                return
            raise DependencyUnavailable("OpenSearch", f"索引初始化失败: {exc}") from exc

    async def health(self) -> tuple[bool, str]:
        """返回 ``(是否可用, 诊断信息)``；探测失败返回 ``(False, 错误)`` 而非抛异常。"""
        try:
            info = await self._client.info()
            return (
                True,
                f"cluster={info.get('cluster_name')} version={info.get('version', {}).get('number')}",
            )
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def count(self) -> int:
        """返回当前索引文档数；探测失败返回 0（不让计数拖垮健康检查）。"""
        try:
            resp = await self._client.count(index=self.index)
            return int(resp.get("count", 0))
        except Exception:  # noqa: BLE001
            return 0

    # ---------------- 写入 ----------------
    def _to_doc(self, chunk: Chunk) -> dict[str, Any]:
        return {
            "chunk_id": chunk.chunk_id,
            "doc_id": chunk.doc_id,
            "doc_title": chunk.doc_title,
            "source": chunk.source,
            "text": chunk.text,
            "chunk_index": chunk.chunk_index,
            "section_path": chunk.section_path,
            "char_start": chunk.char_start,
            "char_end": chunk.char_end,
            "tenant_id": chunk.acl.tenant_id,
            "department_id": chunk.acl.department_id,
            "visibility": chunk.acl.visibility.value,
            "owner": chunk.acl.owner or "",
            "lifecycle": chunk.lifecycle or LIFECYCLE_ACTIVE,
        }

    async def bulk_index(self, chunks: list[Chunk]) -> int:
        """以 chunk_id 为 _id 写入，天然幂等。"""
        if not chunks:
            return 0
        actions = [
            {
                "_op_type": "index",
                "_index": self.index,
                "_id": chunk.chunk_id,
                "_source": self._to_doc(chunk),
            }
            for chunk in chunks
        ]
        try:
            success, errors = await async_bulk(
                self._client, actions, raise_on_error=False, refresh=True
            )
        except Exception as exc:  # noqa: BLE001
            raise DependencyUnavailable("OpenSearch", f"批量写入失败: {exc}") from exc
        if errors:
            logger.warning(
                "OpenSearch 写入存在失败项，成功 %s，失败 %s",
                success,
                len(errors) if isinstance(errors, list) else errors,
            )
        return int(success)

    async def delete_by_doc(self, doc_id: str) -> int:
        """按 ``doc_id`` 删除该文档的全部分块（用于删除文档）；返回实际删除条数。"""
        try:
            resp = await self._client.delete_by_query(
                index=self.index,
                body={"query": {"term": {"doc_id": doc_id}}},
                refresh=True,
                conflicts="proceed",
            )
            return int(resp.get("deleted", 0))
        except Exception as exc:  # noqa: BLE001
            raise DependencyUnavailable("OpenSearch", f"删除失败: {exc}") from exc

    # ---------------- 检索 ----------------
    @staticmethod
    def _compile_filter(filters: FilterDict | None) -> list[dict[str, Any]]:
        """把过滤契约翻译成 OpenSearch bool 查询片段。

        这段是 ACL 在检索侧的**唯一执行点**，与
        :meth:`packages.vectorstores.milvus.MilvusStore._compile_expr` 必须 1:1 对应
        （语义源是 ``packages/retrievers/filters.py`` 的模块 docstring）。

        三个容易踩的点：
            * **用 filter 不用 must**：``filter`` 子句不打分，``must`` 会参与 BM25
              计算。把 tenant/visibility 写进 must 会污染相关性排序。
            * **clause 之间 OR、clause 内部 AND**：visibility_clauses 里每条是
              "可见性 + 归属"的合取，条与条之间是互斥的可见性类别（取其一）。
            * ``minimum_should_match: 1`` 显式写出下限。它在纯 should 下默认值也是 1，
              但一旦有人把这段挪进 must 或改成顶层 should，缺了它过滤会退化成 match-all
              ——这类退化不会报错，只会静默地多召回别人的文档。

        返回空列表等价于 match-all（无过滤），语义与 Milvus 的 ``expr=""`` 一致。
        """
        if not filters:
            return []
        clauses: list[dict[str, Any]] = []
        for field, value in (filters.get("must") or {}).items():
            clauses.append({"term": {field: value}})

        visibility_clauses = filters.get("visibility_clauses") or []
        if visibility_clauses:
            shoulds: list[dict[str, Any]] = []
            for clause in visibility_clauses:
                musts = [{"term": {k: v}} for k, v in clause.items()]
                shoulds.append({"bool": {"filter": musts}})
            clauses.append({"bool": {"should": shoulds, "minimum_should_match": 1}})

        doc_ids = filters.get("doc_ids") or []
        if doc_ids:
            clauses.append({"terms": {"doc_id": doc_ids}})

        # 排除子句（当前用于"已废止文档不参与检索"）。
        # 每条 clause 内部是 AND，条与条之间也是 AND——它们都是"必须不命中"的条件。
        # 注意：未写入该字段的文档**不参与 term 匹配**，因此天然通过排除——
        # 存量文档视为有效，不需要任何数据迁移。
        must_not = filters.get("must_not") or []
        if must_not:
            excludes: list[dict[str, Any]] = []
            for clause in must_not:
                # 空 clause 跳过：否则会生成空的 must_not 子句，语义上无害但会让人以为
                # "确实排除了什么"。与 Milvus 侧的处理保持一致。
                if not clause:
                    continue
                excludes.extend({"term": {k: v}} for k, v in clause.items())
            if excludes:
                clauses.append({"bool": {"must_not": excludes}})
        return clauses

    async def search(
        self,
        query: str,
        *,
        top_k: int = 20,
        filters: FilterDict | None = None,
    ) -> list[SearchHit]:
        """BM25 检索：``multi_match`` 打分 + ``_compile_filter`` 过滤，返回 ``SearchHit`` 列表。

        ``top_k`` 是召回上限（通常大于最终返回给用户的 top_k，留给 RRF/重排收敛）。
        """
        body: dict[str, Any] = {
            "size": top_k,
            "track_total_hits": False,
            "_source": SOURCE_FIELDS,
            "query": {
                "bool": {
                    "must": [
                        {
                            "multi_match": {
                                "query": query,
                                "fields": ["text^1.0", "doc_title^0.5", "section_path^0.3"],
                                "type": "best_fields",
                            }
                        }
                    ],
                    "filter": self._compile_filter(filters),
                }
            },
        }
        try:
            resp = await self._client.search(index=self.index, body=body)
        except Exception as exc:  # noqa: BLE001
            raise DependencyUnavailable("OpenSearch", f"检索失败: {exc}") from exc

        hits: list[SearchHit] = []
        for item in resp.get("hits", {}).get("hits", []):
            src = item.get("_source", {})
            hits.append(
                SearchHit(
                    chunk_id=src.get("chunk_id", item.get("_id", "")),
                    doc_id=src.get("doc_id", ""),
                    doc_title=src.get("doc_title", ""),
                    source=src.get("source", ""),
                    text=src.get("text", ""),
                    chunk_index=int(src.get("chunk_index", 0) or 0),
                    section_path=src.get("section_path", ""),
                    char_start=int(src.get("char_start", 0) or 0),
                    char_end=int(src.get("char_end", 0) or 0),
                    score=float(item.get("_score") or 0.0),
                    retriever=self.name,
                )
            )
        return hits
