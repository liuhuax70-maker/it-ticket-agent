"""检索服务编排：/search（召回+融合）与 /rerank（可选精排）。"""

from __future__ import annotations

from app.config import Settings
from app.filters import build_filters, extract_doc_ids
from app.hybrid import HybridRetriever
from app.opensearch_client import BM25Retriever
from app.rerank import Reranker
from app.vector_client import VectorRetriever
from packages.common.errors import Forbidden
from packages.common.logging import get_logger
from packages.contracts import (
    RerankRequest,
    RerankResponse,
    SearchRequest,
    SearchResponse,
)
from packages.observability.metrics import ACL_MISSING_COUNTER
from packages.retrievers import FilterDict

logger = get_logger("retrieval.service")


class RetrievalService:
    """检索服务编排：编译 ACL 鉴权、调用混合检索、可选重排。

    权限下推的唯一入口是 :meth:`compile_filters`；未携带 ACL 时 fail-closed 拒绝
    （除非显式打开 ``allow_unfiltered_search`` 仅限内部调试），防止全库召回泄露他租户文档。
    """

    def __init__(self, settings: Settings) -> None:
        # 调参文件（configs/retrievers/*.yaml）优先于 .env，便于按环境/域微调召回策略
        settings = settings.with_overrides()
        self._settings = settings
        self._vector = VectorRetriever(settings, settings)
        self._bm25 = BM25Retriever(settings)
        self._hybrid = HybridRetriever(
            self._vector,
            self._bm25,
            rrf_k=settings.rrf_k,
            weight_vector=settings.rrf_weight_vector,
            weight_bm25=settings.rrf_weight_bm25,
            min_score=settings.min_score,
        )
        self._reranker = Reranker(settings.rerank_enabled, settings.rerank_model)

    async def startup(self) -> None:
        await self._vector.ensure()
        await self._bm25.ensure()
        logger.info(
            "retrieval 就绪: mode=%s embed=%s dim=%s rerank=%s",
            self._settings.retrieve_mode,
            self._vector.model_name,
            self._vector.dim,
            self._settings.rerank_enabled,
        )

    def compile_filters(self, req: SearchRequest) -> FilterDict | None:
        """把请求里的 ACL 编译为过滤契约；这是权限下推的唯一入口。"""
        return build_filters(req.acl, extract_doc_ids(req.filters))

    async def search(self, req: SearchRequest) -> SearchResponse:
        mode = req.mode or self._settings.default_mode()
        top_k = req.top_k or self._settings.top_k
        # 权限下推的最终防线：只要请求没带 ACL 就 fail-closed。光看 filters 是否为 None
        # 不够——acl=None 时 compile_filters 仍可能返回带 doc_ids 的契约，而那份契约没有
        # tenant_id，等于跨租户按 doc_id 召回他人文档（doc_ids 是 acl=None 时的逃生口，
        # 必须一并堵住）。正常链路上 ACL 由编排层 route 从网关身份构造，不该为 None；
        # 真为 None 说明身份注入链路断了（网关没注入 / 反向代理丢 header / 有人直连本服务），
        # 这时返回他人的文档比报错危险得多——它不抛异常，只会把别人资料送进生成阶段。
        if req.acl is None:
            if not self._settings.allow_unfiltered_search:
                ACL_MISSING_COUNTER.inc()
                logger.error(
                    "检索请求未携带 ACL，已拒绝（权限下推链路断裂）query=%r", req.query[:60]
                )
                raise Forbidden(
                    "检索请求必须携带 ACL：本服务不做无权限过滤的全库检索。"
                    "确需内部调试请显式设置 ALLOW_UNFILTERED_SEARCH=true"
                )
            logger.warning("检索未携带 ACL，按 allow_unfiltered_search 放行（仅限内部调试）")
        filters = self.compile_filters(req)

        # 重排开启时多取候选：融合结果先截断到 top_k 会让重排失去意义（见 config 说明）。
        candidate_k = (
            max(top_k, self._settings.rerank_candidates) if self._settings.rerank_enabled else top_k
        )
        hits, timings = await self._hybrid.search(
            req.query,
            mode,
            top_k=candidate_k,
            vector_top_k=self._settings.vector_top_k,
            bm25_top_k=self._settings.bm25_top_k,
            filters=filters,
        )
        return SearchResponse(hits=hits, timings_ms=timings)

    async def rerank(self, req: RerankRequest) -> RerankResponse:
        hits, reranker, elapsed = await self._reranker.rerank(req.query, req.hits, req.top_k)
        return RerankResponse(hits=hits, reranker=reranker, timings_ms={"rerank": elapsed})

    async def health(self) -> dict[str, str]:
        ok_v, msg_v = await self._vector.health()
        ok_s, msg_s = await self._bm25.health()
        details = {
            "status": "ok" if (ok_v and ok_s) else "degraded",
            "milvus": msg_v,
            "opensearch": msg_s,
            "embed_model": self._vector.model_name,
            "embed_dim": str(self._vector.dim),
            "mode": self._settings.retrieve_mode,
            "rerank_enabled": str(self._settings.rerank_enabled),
        }
        try:
            details["milvus_rows"] = str(await self._vector.count())
            details["opensearch_rows"] = str(await self._bm25.count())
        except Exception as exc:  # noqa: BLE001
            details["count_error"] = str(exc)
        return details

    async def aclose(self) -> None:
        await self._hybrid.aclose()
