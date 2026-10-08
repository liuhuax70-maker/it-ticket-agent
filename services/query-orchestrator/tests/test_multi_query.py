"""多子查询检索融合的行为测试。

FakeRetrieval 按查询内容返回不同命中：验证 retrieve 节点在
多子查询下真的并发检索了每条子查询、ACL 逐条下发，且融合是
"轮转交织"（每个面向的最佳分块都必须活到候选池）——
这是两版迭代（RRF 全量 -> 分段配额 -> 轮转交织）的最终形态。
"""

from __future__ import annotations

import asyncio
from typing import cast

import pytest
from packages.contracts import (
    ACL,
    SearchHit,
    SearchRequest,
    SearchResponse,
    Visibility,
)
from packages.retrievers import reciprocal_rank_fusion  # noqa: F401 - 节点内部使用，此处导入防误删

from app.clients.retrieval import RetrievalClient
from app.config import Settings
from app.graph.nodes.retrieve import make_retrieve_node


class _PerQueryRetrieval:
    """按查询词路由到不同命中集合的假检索服务。"""

    def __init__(self, routes: dict[str, list[SearchHit]]) -> None:
        self.routes = routes
        self.requests: list[SearchRequest] = []

    async def search(self, req: SearchRequest) -> SearchResponse:
        self.requests.append(req)
        for key, hits in self.routes.items():
            if key in req.query:
                return SearchResponse(hits=hits, timings_ms={"retrieval": 1.0})
        return SearchResponse(hits=[], timings_ms={"retrieval": 1.0})

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


def _hit(chunk_id: str, text: str) -> SearchHit:
    return SearchHit(
        chunk_id=chunk_id,
        doc_id=chunk_id.split(":")[0],
        text=text,
        chunk_index=int(chunk_id.split(":")[1]),
        section_path="s",
        doc_title="t",
        char_start=0,
        char_end=len(text),
        score=0.9,
    )


ACL_OK = ACL(
    tenant_id="default", department_id="hr", visibility=Visibility.department, allowed_roles=[]
)


def _state(sub_queries: list[str]) -> dict:
    return {
        "query": sub_queries[0] if sub_queries else "培训费用由谁审批",
        "sub_queries": sub_queries,
        "acl": ACL_OK,
        "timings": {},
        "errors": [],
    }


def _node(routes: dict[str, list[SearchHit]]):
    """构造节点；fake 用 cast 标明这是刻意的鸭子类型替换（仓库惯例）。"""
    retrieval = cast(RetrievalClient, _PerQueryRetrieval(routes))
    return make_retrieve_node(retrieval, Settings()), cast(_PerQueryRetrieval, retrieval)


def test_single_subquery_keeps_store_order() -> None:
    node, retrieval = _node({"审批": [_hit("d:0", "a"), _hit("d:1", "b")]})
    state = asyncio.run(node(_state(["培训费用审批"])))
    assert [h.chunk_id for h in state["hits"]] == ["d:0", "d:1"]


def test_multi_subquery_interleaves_aspects() -> None:
    """轮转交织：两条子查询各自的第 1 名都必须进入候选池前部。

    这是第一版 RRF 全量融合的教训——只被单路召回的"各面向最佳"
    会被两路都出现的平庸分块挤出候选池（实测 multi-offboard 因此被误拒）。
    """
    node, retrieval = _node(
        {
            "审批": [_hit("d:0", "审批分块"), _hit("d:1", "审批第二")],
            "时限": [_hit("d:2", "时限分块"), _hit("d:0", "审批分块")],
        }
    )
    state = asyncio.run(node(_state(["培训费用由谁审批", "报销时限是多久"])))

    # 两条子查询都被检索（并发完成顺序不定，按集合比较）
    assert {r.query for r in retrieval.requests} == {"报销时限是多久", "培训费用由谁审批"}
    ids = [h.chunk_id for h in state["hits"]]
    assert {"d:0", "d:1", "d:2"} <= set(ids)
    assert state["timings"]["retrieve.sub_queries"] == 2.0


def test_interleave_keeps_both_aspects_in_top_prefix() -> None:
    """截断安全：top-5 前缀里两个面向都有代表（第二版分段配额在这里翻过车）。"""
    routes = {
        "审批": [_hit(f"d_a:{i}", f"审批{i}") for i in range(10)],
        "时限": [_hit(f"d_b:{i}", f"时限{i}") for i in range(10)],
    }
    node, _ = _node(routes)
    state = asyncio.run(node(_state(["培训费用由谁审批", "报销时限是多久"])))
    # candidate_k=20、轮转交织：前 5 条必须是 a/b 交替，不可能全是单面向
    top5_docs = [h.chunk_id.split(":")[0] for h in state["hits"][:5]]
    assert set(top5_docs) == {"d_a", "d_b"}


def test_empty_subquery_falls_back_to_query() -> None:
    node, retrieval = _node({"审批": [_hit("d:0", "a")]})
    state = asyncio.run(node(_state([])))
    assert [h.chunk_id for h in state["hits"]] == ["d:0"]
    assert "retrieve.sub_queries" not in state["timings"], "单查询不应记融合标记"


def test_acl_forwarded_to_every_subquery() -> None:
    node, retrieval = _node({"审批": [_hit("d:0", "a")], "时限": [_hit("d:2", "b")]})
    asyncio.run(node(_state(["培训费用由谁审批", "报销时限是多久"])))
    assert len(retrieval.requests) == 2
    assert all(r.acl == ACL_OK for r in retrieval.requests), "每条子查询都必须带同一份 ACL"