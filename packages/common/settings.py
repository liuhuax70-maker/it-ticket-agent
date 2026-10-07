"""配置基类。

约定（沿用旧 P0 设计）：
    * 所有「拍脑袋的常数」必须进 Settings，禁止在业务代码里硬编码；
    * 读取顺序：真实环境变量 > 根目录 .env > 字段默认值；
    * 字段名与 .env 变量名一一对应（大小写不敏感）。
"""

from __future__ import annotations

from functools import cache
from typing import TypeVar

from pydantic_settings import BaseSettings, SettingsConfigDict

T = TypeVar("T", bound="BaseAppSettings")


class BaseAppSettings(BaseSettings):
    """所有服务 Settings 的公共基类。"""

    model_config = SettingsConfigDict(
        env_file=".env",  # 相对当前工作目录，请在仓库根目录启动服务
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: str = "dev"
    log_level: str = "INFO"
    service_name: str = "unknown"

    # ---- 可观测性（所有服务共用；留空即关闭）----
    langfuse_host: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    otel_enabled: bool = False
    otel_endpoint: str = ""

    # ---- 指标（Prometheus 文本格式，实现见 packages/observability/metrics.py）----
    metrics_enabled: bool = True
    # 非空时 /metrics 要求 `Authorization: Bearer <token>`。
    # 指标会暴露内部路由与流量形态，在没有网络隔离的环境下不应裸奔；
    # 默认空 = 不校验（本地开发方便，生产应设置或只让 Prometheus 网段可达）。
    metrics_token: str = ""


@cache
def _cached(cls: type) -> BaseAppSettings:
    return cls()


def load_settings(cls: type[T]) -> T:
    """进程级单例加载，避免重复解析 .env。"""
    return _cached(cls)  # type: ignore[return-value]


def reset_settings_cache() -> None:
    """测试用：清空单例缓存。"""
    _cached.cache_clear()
