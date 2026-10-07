"""检索必须 fail-closed：请求缺 ACL 时**拒绝**，而不是退化成全库召回。

回归动机：早期实现遇到 ``filters is None`` 只打一条 warning，然后照常做 match-all
检索——即把**所有租户、所有部门**的文档当作检索结果返回。这是权限系统最危险的
失败方向：它不抛异常、不报错，只是安静地把别人的资料送进生成阶段。
"""

from __future__ import annotations

import pytest
from app.config import Settings
from app.service import RetrievalService

from packages.common.errors import Forbidden
from packages.contracts import SearchRequest
from packages.security import Identity


class _RecordingHybrid:
    """记录调用参数、返回空结果的假融合检索器。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def search(self, query: str, mode, **kwargs):  # noqa: ANN001, ANN003, ANN201
        self.calls.append({"query": query, "mode": mode, **kwargs})
        return [], {"hybrid": 0.0}


def _service(*, allow_unfiltered: bool) -> tuple[RetrievalService, _RecordingHybrid]:
    # 用 __new__ 绕开 __init__：真构造会加载 embedding 模型（几百 MB）并建 store 客户端，
    # 而本测试只关心 ACL 闸门。这不是"为了过测试"，而是把被测单元缩到闸门本身。
    service = RetrievalService.__new__(RetrievalService)
    service._settings = Settings(allow_unfiltered_search=allow_unfiltered)
    hybrid = _RecordingHybrid()
    service._hybrid = hybrid  # type: ignore[assignment]  # 假对象只实现 search()，够测闸门
    return service, hybrid


async def test_missing_acl_is_rejected_before_touching_the_stores() -> None:
    service, hybrid = _service(allow_unfiltered=False)
    with pytest.raises(Forbidden, match="必须携带 ACL"):
        await service.search(SearchRequest(query="年假有多少天？"))
    # 关键断言：拒绝必须发生在检索**之前**——先全库召回再丢弃，日志里就留下了痕迹，
    # 而且一旦哪天"丢弃"逻辑被改坏，泄露会立刻发生
    assert hybrid.calls == []


async def test_unfiltered_search_requires_explicit_opt_in() -> None:
    service, hybrid = _service(allow_unfiltered=True)
    await service.search(SearchRequest(query="年假有多少天？"))
    assert len(hybrid.calls) == 1
    assert hybrid.calls[0]["filters"] is None


async def test_acl_is_compiled_and_pushed_down_to_the_stores() -> None:
    service, hybrid = _service(allow_unfiltered=False)
    acl = Identity(
        user_id="u_alice", tenant_id="default", department_id="hr", roles=["rag_reader"]
    ).to_acl(owner=True)

    await service.search(SearchRequest(query="年假", acl=acl))

    assert len(hybrid.calls) == 1
    filters = hybrid.calls[0]["filters"]
    assert filters is not None, "带 ACL 的请求必须把过滤条件下推到存储层"
