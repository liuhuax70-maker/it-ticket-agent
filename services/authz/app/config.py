"""authz 配置。"""

from __future__ import annotations

from packages.common.constants import SERVICE_AUTHZ
from packages.security import SecuritySettings


class Settings(SecuritySettings):
    service_name: str = SERVICE_AUTHZ
    host: str = "0.0.0.0"
    port: int = 8008

    # 策略包目录（compose 里挂进 OPA 容器的是同一份文件）
    policies_dir: str = "services/authz/policies"
    decision_path: str = "v1/data/rag/allow"
    reason_path: str = "v1/data/rag/reason"
