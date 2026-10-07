"""OpenTelemetry 初始化占位。

当前最小闭环不引入 OTel collector；本函数先把接入点固定下来，
避免后续在 6 个服务里各写一套初始化代码。

启用步骤（后续阶段）：
    1. 安装 ``opentelemetry-sdk``、``opentelemetry-exporter-otlp``、
       ``opentelemetry-instrumentation-fastapi``、``opentelemetry-instrumentation-httpx``；
    2. 在 ``init_otel`` 内创建 TracerProvider + OTLPSpanExporter；
    3. 各服务 ``main.py`` 的 app 上挂 ``FastAPIInstrumentor``。
"""

from __future__ import annotations

from packages.common.logging import get_logger

logger = get_logger("observability.otel")


def init_otel(service_name: str, endpoint: str | None = None, enabled: bool = False) -> bool:
    """返回是否真正启用了 OTel。当前恒为 False（占位）。"""
    if not enabled:
        logger.debug("OTel 未启用: service=%s", service_name)
        return False
    logger.warning(
        "init_otel 目前是占位实现，未接入 exporter（service=%s endpoint=%s）", service_name, endpoint
    )
    return False
