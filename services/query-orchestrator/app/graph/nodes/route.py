"""路由节点：把身份编译成权限上下文。

权限上下文（ACL）在这里产出并一路下传到 retrieval —— 编排层不参与
过滤计算，只负责**如实传递**请求方身份，过滤语义唯一实现在共享库。
"""

from __future__ import annotations

import time

from app.config import Settings
from app.graph.nodes.base import merge_timing
from app.graph.state import RAGState
from packages.common.logging import get_logger
from packages.security import Identity

logger = get_logger("orchestrator.node.route")


def make_route_node(settings: Settings):
    """构造路由节点：从身份编译 ACL（**权限下推起点**）。

    检索模式（mode）已在入口节点（``OrchestratorService.chat``）定好并写入 state，
    这里不再改写——缓存键含 mode 分量，若在此改 mode 会让读/写缓存键对不上。
    """

    async def route(state: RAGState) -> dict:
        started = time.perf_counter()
        identity = Identity(
            user_id=state.get("user_id") or settings.default_user_id,
            tenant_id=state.get("tenant_id") or settings.default_tenant_id,
            department_id=state.get("department_id") or settings.default_department_id,
            roles=list(state.get("roles") or []),
        )
        # owner=True：让 private 可见性分支生效，用户能检索到自己的私有文档
        acl = identity.to_acl(owner=True)
        return merge_timing(state, "route", started, acl=acl)

    return route
