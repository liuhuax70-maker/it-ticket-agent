"""query-orchestrator 配置。

本服务**不直连任何存储**：只通过 HTTP 调用 retrieval 与 model-gateway，
因此配置里没有 Milvus / OpenSearch / Postgres 字段——依赖收窄到两跳。
"""

from __future__ import annotations

from packages.common.constants import SERVICE_QUERY_ORCHESTRATOR
from packages.common.settings import BaseAppSettings
from packages.contracts import RetrieveMode


class Settings(BaseAppSettings):
    service_name: str = SERVICE_QUERY_ORCHESTRATOR
    host: str = "0.0.0.0"
    port: int = 8001

    # ---- 下游服务 ----
    retrieval_url: str = "http://localhost:8002"
    model_gateway_url: str = "http://localhost:8003"
    request_timeout: float = 120.0

    # ---- 编排参数 ----
    top_k: int = 5
    retrieve_mode: str = RetrieveMode.hybrid.value
    # 召回条数：先多召回再由重排/截断收敛到 top_k
    candidate_k: int = 10

    # ---- 可选节点开关（默认关闭，保持可解释的最小链路）----
    rewrite_enabled: bool = False
    rerank_enabled: bool = False
    guard_llm_enabled: bool = False
    cache_enabled: bool = False
    cache_ttl_seconds: int = 3600
    redis_url: str = "redis://localhost:6379/0"

    # ---- 最大追问/兜底 ----
    # 检索为空时直接拒答，不调用 LLM（省一次调用，也彻底堵死幻觉）。
    # ⚠️ 当前是**死配置**：图边（graph/edges.py 的 after_retrieve）无条件转到 refuse
    # 节点，没有任何地方读它。设 REFUSE_ON_EMPTY=false 期望恢复"检索为空也作答"
    # 是无效的，且不会有任何提示——而这恰好是幻觉高发的配置。
    # 要启用这个开关，必须先把它接到 edges.after_retrieve 上。
    refuse_on_empty: bool = True

    # ---- 身份默认值（鉴权开启后由网关透传真实身份）----
    default_tenant_id: str = "default"
    default_department_id: str = "default"
    default_user_id: str = "u_demo"

    def default_mode(self) -> RetrieveMode:
        try:
            return RetrieveMode(self.retrieve_mode)
        except ValueError:
            return RetrieveMode.hybrid
