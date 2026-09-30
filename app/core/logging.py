"""统一日志配置。

输出到 stdout（容器友好），格式包含时间、级别、模块名，
便于在 docker logs 中按模块检索。
"""

import logging
import sys

_CONFIGURED = False


#: 降噪：这些库在 INFO 级别会把每个 HTTP 请求都打出来，淹没业务日志
_NOISY_LOGGERS = ("httpx", "httpcore", "urllib3", "pymilvus", "asyncio", "aiosqlite")


def setup_logging(level: str = "INFO") -> None:
    """初始化根 logger，仅执行一次。"""
    global _CONFIGURED
    if _CONFIGURED:
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """获取模块 logger。"""
    return logging.getLogger(name)
