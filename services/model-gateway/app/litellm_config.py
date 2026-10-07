"""LiteLLM 模型清单加载。

配置文件是**声明式的模型台账**（``configs/models/litellm.yaml``），
用于：1) 暴露 ``/models`` 给运维查看；2) 说明各 provider 的切换方式。
实际调用走 ``packages.llms``，避免与 LiteLLM Router 双份状态。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from packages.common.errors import ConfigError
from packages.common.logging import get_logger
from packages.contracts import ModelInfo

logger = get_logger("model_gateway.litellm_config")


@lru_cache(maxsize=4)
def load_model_config(path: str) -> dict[str, Any]:
    file = Path(path)
    if not file.exists():
        logger.warning("LiteLLM 配置不存在: %s（/models 将只返回当前生效模型）", file)
        return {}
    try:
        data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(f"解析 {file} 失败: {exc}") from exc
    return data


def declared_models(path: str) -> list[ModelInfo]:
    """把配置文件里的 model_list 转成契约对象。"""
    data = load_model_config(path)
    result: list[ModelInfo] = []
    for entry in data.get("model_list", []) or []:
        name = entry.get("model_name", "")
        params = entry.get("litellm_params", {}) or {}
        model = params.get("model", "")
        result.append(
            ModelInfo(
                name=name,
                provider=str(model).split("/", 1)[0] if "/" in str(model) else "openai",
                kind="chat",
                available=bool(name),
                note=entry.get("note"),
            )
        )
    return result
