"""安全与鉴权配置。"""

from __future__ import annotations

from packages.common.settings import BaseAppSettings


class SecuritySettings(BaseAppSettings):
    service_name: str = "api-gateway"

    # false = 使用固定身份（最小闭环）；true = 校验 Keycloak JWT + OPA 决策
    authz_enabled: bool = False

    keycloak_url: str = "http://localhost:8180"
    keycloak_realm: str = "rag"
    keycloak_client_id: str = "rag-api"
    keycloak_audience: str = ""
    # 生产保持 True。仅在开发环境因主机名（127.0.0.1 / localhost / 容器名）
    # 不一致导致 iss 对不上、且暂时无法统一 KC_HOSTNAME 时才关闭。
    keycloak_verify_issuer: bool = True

    opa_url: str = "http://localhost:8181"
    # 查整个 package 而不是单查 /allow：单查 allow 只返回布尔，
    # 拒绝原因（reason）会丢失，审计时无法回答"为什么被拦"。
    opa_decision_path: str = "v1/data/rag"
    opa_timeout: float = 3.0

    # 无需身份即可访问的路径（探针、静态资源、文档）。
    # 注意：这些路径不会注入身份，因此也不能访问需要身份的接口。
    authz_exempt_paths: str = "/health,/openapi.json,/docs,/redoc,/ui"

    def exempt_paths(self) -> tuple[str, ...]:
        return tuple(p.strip() for p in self.authz_exempt_paths.split(",") if p.strip())

    # 固定身份占位（AUTHZ_ENABLED=false 时生效）
    default_user_id: str = "u_demo"
    default_tenant_id: str = "default"
    default_department_id: str = "default"

    def jwks_url(self) -> str:
        return f"{self.keycloak_url.rstrip('/')}/realms/{self.keycloak_realm}/protocol/openid-connect/certs"

    def issuer(self) -> str:
        return f"{self.keycloak_url.rstrip('/')}/realms/{self.keycloak_realm}"
