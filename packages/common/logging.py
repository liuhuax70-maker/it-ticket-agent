"""结构化日志初始化。"""

from __future__ import annotations

import logging
import logging.config

_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"


def setup_logging(service: str, level: str = "INFO") -> None:
    """幂等初始化根 logger；重复调用只更新级别。"""
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {"default": {"format": _FORMAT, "datefmt": "%Y-%m-%d %H:%M:%S"}},
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                    "stream": "ext://sys.stdout",
                }
            },
            "root": {"handlers": ["console"], "level": level.upper()},
            "loggers": {
                # 降低第三方噪声
                "httpx": {"level": "WARNING"},
                "httpcore": {"level": "WARNING"},
                "urllib3": {"level": "WARNING"},
                "opensearch": {"level": "WARNING"},
                "LiteLLM": {"level": "WARNING"},
                "litellm": {"level": "WARNING"},
                "pymilvus": {"level": "WARNING"},
                "sqlalchemy.engine": {"level": "WARNING"},
            },
        }
    )
    logging.getLogger(service).debug("logging configured: level=%s", level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
