"""api-gateway 配置：入口层只关心「鉴权 + 限流 + 转发」。"""

from __future__ import annotations

from packages.common.constants import SERVICE_API_GATEWAY
from packages.security import SecuritySettings


class Settings(SecuritySettings):
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
