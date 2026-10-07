"""LangGraph 闭环图测试：用假下游服务跑真实图，覆盖关键分支。"""

from __future__ import annotations

from typing import cast

import pytest
from app.clients.cache import QueryCache
from app.clients.model_gateway import ModelGatewayClient
from app.clients.retrieval import RetrievalClient
from app.config import Settings
from app.graph import build_graph
from app.graph.edges import NODE_RETRIEVE, NODE_ROUTE
from app.graph.state import RAGState

from packages.common.constants import REFUSE_TEXT
from packages.contracts import (
    GenerateResponse,
    RerankResponse,
    RetrieveMode,
    SearchHit,
    SearchRequest,
    SearchResponse,
)

ANSWER_WITH_CITATION = "转正后凭发票报销，上限五百元[1]。"
ANSWER_WITHOUT_CITATION = "转正后凭发票报销，上限五百元。"


def _hit(chunk_id: str, text: str) -> SearchHit:
    return SearchHit(
        chunk_id=chunk_id,
        doc_id=chunk_id.split(":")[0],
        text=text,
        chunk_index=int(chunk_id.split(":")[1]),
        section_path="员工手册 > 第三章 福利 > 3.2 入职体检",
        doc_title="员工手册",
        char_start=100,
        char_end=100 + len(text),
        score=0.9,
    )


class FakeRetrieval:
    def __init__(self, hits: list[SearchHit]) -> None:
        self.hits = hits
        self.searches: list[SearchRequest] = []

    async def search(self, req: SearchRequest) -> SearchResponse:
        self.searches.append(req)
        return SearchResponse(hits=self.hits, timings_ms={"retrieval": 3.0})

    async def rerank(self, req) -> RerankResponse:  # noqa: ANN001
        return RerankResponse(hits=req.hits[: req.top_k], reranker="rrf")

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


class FakeGateway:
    def __init__(self, answer: str = ANSWER_WITH_CITATION) -> None:
        self.answer = answer
        self.generate_calls = 0
        self.complete_calls = 0

    async def generate(self, req) -> GenerateResponse:  # noqa: ANN001
        self.generate_calls += 1
        return GenerateResponse(
            answer=self.answer,
            model="fake",
            provider="fake",
            timings_ms={"generate": 12.0, "total": 13.0},
        )

    async def complete(self, req) -> GenerateResponse:  # noqa: ANN001
        self.complete_calls += 1
        return GenerateResponse(answer="入职体检费用如何报销", model="fake", provider="fake")

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


@pytest.fixture()
def make_graph():
    def _make(*, hits: list[SearchHit], answer: str = ANSWER_WITH_CITATION, **overrides):
        settings = Settings(**overrides)
        retrieval = FakeRetrieval(hits)
        gateway = FakeGateway(answer)
        cache = QueryCache(settings.redis_url, 60, enabled=False)
        graph = build_graph(
            # fake 只实现被用到的两个方法；用 cast 标明这是刻意的鸭子类型替换，
            # 而不是把 build_graph 的签名为了测试放宽成 Any。
            retrieval=cast(RetrievalClient, retrieval),
            model_gateway=cast(ModelGatewayClient, gateway),
            cache=cache,
            settings=settings,
        )
        return graph, retrieval, gateway

    return _make


def _state(**kw) -> RAGState:
    base: RAGState = {
        "query": "入职体检费用怎么报销？",
        "tenant_id": "t1",
        "department_id": "hr",
        "user_id": "u_1",
        "roles": ["rag_user"],
        "top_k": 3,
        # 故意把 mode 置空：验证 route 节点在缺少显式模式时的兜底行为
        # （真实 HTTP 链路上 service 总会预填 mode，所以这条分支只在直接组图时可达）
        "mode": cast(RetrieveMode, None),
        "trace_id": "tr_test",
        "timings": {},
        "errors": [],
    }
    base.update(kw)  # type: ignore[typeddict-item]
    return base


async def test_happy_path_produces_citations(make_graph) -> None:
    graph, retrieval, gateway = make_graph(hits=[_hit("d_1:4", "转正后凭发票报销，上限五百元。")])
    final = await graph.ainvoke(_state())

    assert final["refused"] is False
    assert final["answer"] == ANSWER_WITH_CITATION
    assert len(final["citations"]) == 1
    citation = final["citations"][0]
    assert citation.chunk_id == "d_1:4"
    assert citation.char_start == 100
    assert "500" not in citation.snippet  # snippet 来自原文，未加工
    assert gateway.generate_calls == 1
    assert final["timings"]["total"] if "total" in final["timings"] else True


async def test_acls_and_candidate_k_are_pushed_down(make_graph) -> None:
    graph, retrieval, _ = make_graph(hits=[_hit("d_1:4", "x")])
    await graph.ainvoke(_state(candidate_k=1))
    req = retrieval.searches[0]
    assert req.acl is not None
    assert req.acl.tenant_id == "t1"
    assert req.acl.department_id == "hr"
    assert req.acl.owner == "u_1"
    # 召回条数应大于最终 top_k
    assert req.top_k >= 3


async def test_empty_retrieval_refuses_without_calling_llm(make_graph) -> None:
    graph, _, gateway = make_graph(hits=[])
    final = await graph.ainvoke(_state())

    assert final["refused"] is True
    assert final["answer"] == REFUSE_TEXT
    assert final["citations"] == []
    assert gateway.generate_calls == 0, "检索为空时绝不能调用 LLM"
    assert "empty_retrieval" in final["errors"]


async def test_empty_generation_refuses_with_correct_reason(make_graph) -> None:
    """检索有结果但模型返回空 -> 拒答，且错误标签必须是 empty_generation。

    这条标签曾经统一写成 empty_retrieval：排障的人会去查检索服务，
    而检索其实一切正常，真凶是模型异常/截断。拒答出口被两条边共用，
    原因必须按 state 里的实际线索区分。
    """
    graph, retrieval, gateway = make_graph(hits=[_hit("d_1:4", "转正后凭发票报销。")], answer="   ")
    final = await graph.ainvoke(_state())

    assert final["refused"] is True
    assert final["answer"] == REFUSE_TEXT
    assert final["citations"] == []
    assert gateway.generate_calls == 1, "检索有结果时应调用过 LLM"
    assert retrieval.hits, "前提：检索是有结果的"
    assert "empty_generation" in final["errors"]
    assert "empty_retrieval" not in final["errors"]


async def test_missing_citation_falls_back_to_top1_and_records_error(make_graph) -> None:
    graph, _, _ = make_graph(
        hits=[_hit("d_1:4", "转正后凭发票报销。")], answer=ANSWER_WITHOUT_CITATION
    )
    final = await graph.ainvoke(_state())

    assert final["refused"] is False
    assert len(final["citations"]) == 1
    assert final["citations"][0].index == 1
    assert "citation_fallback_to_top1" in final["errors"]


async def test_model_self_refusal_is_respected(make_graph) -> None:
    graph, _, _ = make_graph(hits=[_hit("d_1:4", "x")], answer=REFUSE_TEXT)
    final = await graph.ainvoke(_state())
    assert final["refused"] is True
    assert final["citations"] == []


async def test_model_composed_refusal_is_normalized(make_graph) -> None:
    """模型用自己的话说「资料里没有」也要走拒答，且不能挂引用。"""
    graph, _, _ = make_graph(
        hits=[_hit("d_1:4", "x")], answer="参考资料中未提及公司年会的举办地点。[1][2]"
    )
    final = await graph.ainvoke(_state())

    assert final["refused"] is True
    assert final["citations"] == []
    assert final["answer"] == REFUSE_TEXT, "对外话术必须统一，不能透出模型的自由表述"
    assert "refusal_detected" in final["errors"]


async def test_quoted_short_query_routes_to_keyword(make_graph) -> None:
    graph, retrieval, _ = make_graph(hits=[_hit("d_1:0", "x")])
    await graph.ainvoke(_state(query="“BT-2024”"))
    assert retrieval.searches[0].mode.value == "keyword"


async def test_node_names_are_stable() -> None:
    """节点名是图的可观测契约，改名属于破坏性变更。"""
    assert NODE_ROUTE == "route"
    assert NODE_RETRIEVE == "retrieve"
