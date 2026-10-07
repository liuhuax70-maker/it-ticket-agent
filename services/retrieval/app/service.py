"""检索服务编排：/search（召回+融合）与 /rerank（可选精排）。"""

from __future__ import annotations

from app.config import Settings
from app.filters import build_filters, extract_doc_ids
from app.hybrid import HybridRetriever
from app.opensearch_client import BM25Retriever
from app.rerank import Reranker
from app.vector_client import VectorRetriever
from packages.common.logging import get_logger
from packages.contracts import (
    RerankRequest,
    RerankResponse,
    SearchRequest,
    SearchResponse,
)
from packages.retrievers import FilterDict

logger = get_logger("retrieval.service")


class RetrievalService:
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
        filters = self.compile_filters(req)
        if filters is None:
            # 这是**未受控降级**：filters=None 会被两个 store 翻译成 match-all，
            # 即不分租户、不分部门的全库召回，且不会报错。检索端口可被直连，
            # 所以这条 warning 是唯一的告警信号——不要在重构里把它删掉或降级为 debug。
            # 正常链路上 ACL 由编排层 route 节点从网关身份构造，不应出现 None；
            # 若真出现，说明身份注入链路断了（网关没注入 / 上游漏传 header）。
            logger.warning("检索未携带 ACL，本次不做权限过滤（仅限内部调试场景）")

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
        # ⚠️ 这里只关了 BM25 客户端，**Milvus 客户端没有关闭**（向量客户端由
        # HybridRetriever 持有，未暴露关闭入口）。进程退出时由 OS 回收，不影响
        # 正确性；但"资源释放不完整"这件事必须写下来，否则重构时容易误以为已经关干净。
        await self._bm25.aclose()
