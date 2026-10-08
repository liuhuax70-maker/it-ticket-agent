"""authz 配置。"""

from __future__ import annotations

from packages.common.constants import SERVICE_AUTHZ
from packages.security import SecuritySettings


class Settings(SecuritySettings):
    """authz 配置（控制面）：继承安全配置，含 OPA 策略目录与决策路径。

    本服务不参与请求链路（策略执行点在 api-gateway），挂掉不影响问答可用性。
    """

    service_name: str = SERVICE_AUTHZ
    host: str = "0.0.0.0"
    port: int = 8008

    # 策略包目录（compose 里挂进 OPA 容器的是同一份文件）
    policies_dir: str = "services/authz/policies"
    decision_path: str = "v1/data/rag/allow"
    reason_path: str = "v1/data/rag/reason"
