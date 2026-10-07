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

    # ---- 上传 ----
    max_upload_mb: int = 32

    # ---- 前端 ----
    serve_ui: bool = True
    cors_origins: str = "http://localhost:3000,http://localhost:8000"

    def exempt_paths(self) -> set[str]:
        return {p.strip() for p in self.rate_limit_exempt_paths.split(",") if p.strip()}

    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]
