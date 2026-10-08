"""api-gateway 配置：入口层只关心「鉴权 + 限流 + 转发」。"""

from __future__ import annotations

from packages.common.constants import SERVICE_API_GATEWAY
from packages.security import SecuritySettings


class Settings(SecuritySettings):
    """api-gateway 配置：入口层只关心「鉴权 + 限流 + 转发」。

    下游地址、Redis 限流、审计落库、上传/前端开关都在此；``rate_limit_exempt`` 与父类
    ``authz_exempt_paths`` 是两个不同名单（方法名刻意不同，避免覆盖静默失效）。
    """

    service_name: str = SERVICE_API_GATEWAY
    host: str = "0.0.0.0"
    port: int = 8000

    # ---- 下游服务 ----
    query_orchestrator_url: str = "http://localhost:8001"
    ingestion_url: str = "http://localhost:8004"
    model_gateway_url: str = "http://localhost:8003"
    feedback_url: str = "http://localhost:8007"
    # 上传大文件 + 解析 + 入库链路较长，超时给足
    ingest_timeout: float = 600.0
    request_timeout: float = 180.0

    # ---- Redis（限流计数）----
    redis_url: str = "redis://localhost:6379/0"

    # ---- 限流 ----
    rate_limit_enabled: bool = True
    rate_limit_per_minute: int = 120
    # 这些路径不计入限流（探针与静态资源）
    rate_limit_exempt_paths: str = "/health,/ui,/openapi.json,/docs,/redoc"

    # ---- 审计 ----
    audit_enabled: bool = True
    # 审计落库（Postgres）。留空 = 只打 stdout 日志不落库。
    # 落库失败不影响业务请求（AuditSink 丢弃并告警日志），所以默认开启。
    audit_database_url: str = "postgresql+asyncpg://rag:rag@localhost:5432/rag"

    # ---- 上传 ----
    max_upload_mb: int = 32

    # ---- 前端 ----
    serve_ui: bool = True
    cors_origins: str = "http://localhost:3000,http://localhost:8000"

    # 浏览器侧 OIDC 客户端（public client，走 PKCE，无 secret）。
    #
    # 刻意与 SecuritySettings.keycloak_client_id（= rag-api，confidential client，
    # 服务间调用用，带 secret）**分开**：把 secret-bearing 的 client_id 暴露给前端
    # 毫无意义，而把浏览器 client 配到后端去又会让人误以为前端需要 secret。
    # realm 的 rag-ui 定义见 infra/docker/keycloak/realm-rag.json。
    keycloak_ui_client_id: str = "rag-ui"

    def rate_limit_exempt(self) -> set[str]:
        """限流豁免名单。

        ⚠️ 这里**不要**再叫 ``exempt_paths()``：父类（SecuritySettings）有一个同名方法
        表示"免鉴权名单"，子类一旦覆盖它，``AUTHZ_EXEMPT_PATHS`` 就会静默失效
        （两个中间件都只会拿到限流名单）。两个名单的默认值恰好相同，所以这种覆盖
        长期无症状，直到有人去改那个安全配置。
        """
        return {p.strip() for p in self.rate_limit_exempt_paths.split(",") if p.strip()}

    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]
