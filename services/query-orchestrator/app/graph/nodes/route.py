"""路由节点：决定检索模式，并把身份编译成权限上下文。

权限上下文（ACL）在这里产出并一路下传到 retrieval —— 编排层不参与
过滤计算，只负责**如实传递**请求方身份，过滤语义唯一实现在共享库。
"""

from __future__ import annotations

import re
import time

from app.config import Settings
from app.graph.nodes.base import merge_timing
from app.graph.state import RAGState
from packages.common.logging import get_logger
from packages.contracts import RetrieveMode
from packages.security import Identity

logger = get_logger("orchestrator.node.route")

# 带引号的短术语通常是精确术语（制度编号、错误码），BM25 比向量更稳
_QUOTED = re.compile(r'["“”「」\']')


def make_route_node(settings: Settings):
    async def route(state: RAGState) -> dict:
        started = time.perf_counter()
        query = state.get("rewritten_query") or state.get("query", "")

        mode = state.get("mode")
        if mode is None:
            mode = settings.default_mode()
            if (
                settings.retrieve_mode == RetrieveMode.hybrid.value
                and _QUOTED.search(query)
                and len(query) <= 16
            ):
                logger.debug("检测到精确术语查询，改走 BM25: %r", query)
                mode = RetrieveMode.keyword

        identity = Identity(
            user_id=state.get("user_id") or settings.default_user_id,
            tenant_id=state.get("tenant_id") or settings.default_tenant_id,
            department_id=state.get("department_id") or settings.default_department_id,
            roles=list(state.get("roles") or []),
        )
        # owner=True：让 private 可见性分支生效，用户能检索到自己的私有文档
        acl = identity.to_acl(owner=True)

        return merge_timing(state, "route", started, mode=mode, acl=acl)

    return route
