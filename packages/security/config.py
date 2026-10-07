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

    opa_url: str = "http://localhost:8181"
    opa_decision_path: str = "v1/data/rag/allow"
    opa_timeout: float = 3.0

    # 固定身份占位（AUTHZ_ENABLED=false 时生效）
    default_user_id: str = "u_demo"
    default_tenant_id: str = "default"
    default_department_id: str = "default"

    def jwks_url(self) -> str:
        return f"{self.keycloak_url.rstrip('/')}/realms/{self.keycloak_realm}/protocol/openid-connect/certs"

    def issuer(self) -> str:
        return f"{self.keycloak_url.rstrip('/')}/realms/{self.keycloak_realm}"
