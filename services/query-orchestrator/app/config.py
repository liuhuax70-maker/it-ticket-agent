"""query-orchestrator 配置。

本服务**不直连任何存储**：只通过 HTTP 调用 retrieval 与 model-gateway，
因此配置里没有 Milvus / OpenSearch / Postgres 字段——依赖收窄到两跳。
"""

from __future__ import annotations

from packages.common.constants import SERVICE_QUERY_ORCHESTRATOR
from packages.common.settings import BaseAppSettings
from packages.contracts import RetrieveMode


class Settings(BaseAppSettings):
    """编排服务配置：只依赖两跳下游（retrieval / model-gateway），不含任何存储字段。

    含各**可选节点开关**（改写/规划/重排/守卫/缓存，默认全关以保证最小可解释链路）、
    缓存版本号（换配置后必须调大）与身份默认值（鉴权开启后由网关透传真实身份）。
    """

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

    # ---- 查询规划（子查询分解）----
    # 默认关闭：多花一次 LLM 调用，增益要靠评测证明（多跳样本片段召回）。
    # 启发式闸门保证单信息点问题不产生额外调用。
    decompose_enabled: bool = False
    decompose_max_sub_queries: int = 3
    rerank_enabled: bool = False
    guard_llm_enabled: bool = False
    cache_enabled: bool = False
    cache_ttl_seconds: int = 3600
    redis_url: str = "redis://localhost:6379/0"
    # 缓存版本号，参与缓存键。
    #
    # **改变任何影响答案内容的配置后必须调大它**：更换作答模型（LLM_PROVIDER）、
    # 改提示词、改切分参数、改 top_k 语义等。否则新配置会继续命中旧配置产出的答案，
    # 直到 TTL 过期——症状是"配置改了但没生效"，而且在 TTL 内新旧答案混在一起，
    # 评测结果无法归因（报告里的 answer_model 会暴露这种混用）。
    #
    # 之所以用显式版本号而不是把模型名拼进键：编排层并不知道 model-gateway 实际用哪个模型
    # （那是下游的配置），拼一个自己都不知道的值只会制造假的安全感。
    cache_version: str = "1"

    # ---- 身份默认值（鉴权开启后由网关透传真实身份）----
    default_tenant_id: str = "default"
    default_department_id: str = "default"
    default_user_id: str = "u_demo"

    def default_mode(self) -> RetrieveMode:
        try:
            return RetrieveMode(self.retrieve_mode)
        except ValueError:
            return RetrieveMode.hybrid
