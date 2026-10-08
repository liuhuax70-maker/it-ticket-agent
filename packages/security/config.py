"""安全与鉴权配置。"""

from __future__ import annotations

from packages.common.settings import BaseAppSettings


class SecuritySettings(BaseAppSettings):
    """鉴权与安全相关配置（api-gateway 使用）。

    核心开关是 ``authz_enabled``：关 = 固定身份（最小闭环），开 = 校验 Keycloak JWT + OPA 决策。
    所有 URL/超时都是可配置项，默认指向本机 Keycloak/OPA（见 ``docker-compose.yml`` 的 profile）。
    """

    service_name: str = "api-gateway"

    # false = 使用固定身份（最小闭环）；true = 校验 Keycloak JWT + OPA 决策
    authz_enabled: bool = False

    keycloak_url: str = "http://localhost:8180"
    keycloak_realm: str = "rag"
    keycloak_client_id: str = "rag-api"
    keycloak_client_secret: str = "rag-api-dev-secret"
    keycloak_audience: str = ""
    # 生产保持 True。仅在开发环境因主机名（127.0.0.1 / localhost / 容器名）
    # 不一致导致 iss 对不上、且暂时无法统一 KC_HOSTNAME 时才关闭。
    keycloak_verify_issuer: bool = True

    opa_url: str = "http://localhost:8181"
    # 决策入口：查整个 package（``v1/data/rag``）而不是单查 ``/allow``，
    # 因为单查 allow 只返回布尔，拒绝原因（reason）会丢失，审计时无法回答"为什么被拦"。
    # 注意：真正调用时仍按 ``decision_path`` / ``reason_path`` 两次取字段
    # （见 ``services/authz``），``v1/data/rag`` 只是它们的前缀。
    opa_decision_path: str = "v1/data/rag"
    opa_timeout: float = 3.0

    # 无需身份即可访问的路径（探针、静态资源、文档）。
    # 注意：这些路径不会注入身份，因此也不能访问需要身份的接口。
    authz_exempt_paths: str = "/health,/openapi.json,/docs,/redoc,/ui"

    def authz_exempt(self) -> tuple[str, ...]:
        """免鉴权路径。命名带 ``authz_`` 前缀是为了避免与限流豁免名单同名覆盖——
        历史上两边都叫 ``exempt_paths()``，子类（网关）覆盖了父类实现，
        导致 ``AUTHZ_EXEMPT_PATHS`` 这个安全配置**完全不生效且不报错**。
        """
        paths = [p.strip() for p in self.authz_exempt_paths.split(",") if p.strip()]

        # 根路径无条件豁免（**不受配置控制**）。
        #
        # 原因：网关把 UI 挂在 /ui，根路径只做一次 302 跳转到 /ui/，自身不返回任何数据，
        # 放行不构成越权风险。但它一旦被拦，用户拿到的就是 401 JSON —— 而登录页在 /ui，
        # 浏览器根本到不了，等于"开启鉴权后无法登录"的死锁。
        #
        # 为什么不能只写进 authz_exempt_paths 的默认值：已经部署的环境 .env 里
        # 早就显式配好了这个列表，改默认值对他们无效，必须在这里兜住。
        #
        # 注意中间件按 `path == item or path.startswith(item + "/")` 匹配，
        # item="/" 时前缀是 "//"，所以只有精确的 "/" 命中，**不会**顺带放行 /chat 等接口。
        if "/" not in paths:
            paths.append("/")

        # /metrics 只在**已配置 METRICS_TOKEN** 时才免用户鉴权：
        #   * Prometheus 没有 Keycloak 令牌，若要求用户鉴权就没法抓取指标；
        #   * 但网关是公网入口，「免鉴权 + 无 token」等于任何人可读内部路由与流量形态，
        #     所以默认不放行——想抓网关指标，就配上 METRICS_TOKEN，这是一次显式决定。
        # 内部服务没有这道鉴权中间件，/metrics 本来就可达，不受这里影响。
        if self.metrics_enabled and self.metrics_token and "/metrics" not in paths:
            paths.append("/metrics")
        return tuple(paths)

    # 固定身份占位（AUTHZ_ENABLED=false 时生效）
    default_user_id: str = "u_demo"
    default_tenant_id: str = "default"
    default_department_id: str = "default"

    def jwks_url(self) -> str:
        return f"{self.keycloak_url.rstrip('/')}/realms/{self.keycloak_realm}/protocol/openid-connect/certs"

    def issuer(self) -> str:
        return f"{self.keycloak_url.rstrip('/')}/realms/{self.keycloak_realm}"

    def token_url(self) -> str:
        return f"{self.issuer()}/protocol/openid-connect/token"
