"""提示模板注册表。

模板放在 ``configs/prompts/<name>.<version>.txt``，变量占位符为 ``{{var}}``。
选择 ``{{var}}`` 而不是 str.format/str.Template，是为了避免提示里出现的
``{}`` 或 ``$`` 被误解析。
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from packages.common.errors import ConfigError

_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")

_DEFAULT_ROOT = Path(__file__).resolve().parents[2] / "configs" / "prompts"


class PromptRegistry:
    """提示模板注册表：按 ``(name, version)`` 加载 ``configs/prompts`` 下的模板并渲染。

    渲染时强制变量校验——缺变量直接 ``ConfigError``，而不是把 ``{{var}}`` 原样发给模型。
    """

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root else _DEFAULT_ROOT

    def path_of(self, name: str, version: str = "v1") -> Path:
        return self.root / f"{name}.{version}.txt"

    def get(self, name: str, version: str = "v1") -> str:
        path = self.path_of(name, version)
        if not path.exists():
            raise ConfigError(f"提示模板不存在: {path}")
        return path.read_text(encoding="utf-8")

    def variables(self, name: str, version: str = "v1") -> set[str]:
        return set(_PLACEHOLDER.findall(self.get(name, version)))

    def render(self, name: str, version: str = "v1", **variables: object) -> str:
        """渲染并做变量校验：缺变量直接报错，而不是把 {{var}} 原样发给模型。"""
        template = self.get(name, version)
        required = set(_PLACEHOLDER.findall(template))
        provided = set(variables)
        missing = required - provided
        if missing:
            raise ConfigError(
                f"提示模板 {name}.{version} 缺少变量: {sorted(missing)}（已提供: {sorted(provided)}）"
            )
        rendered = template
        for key, value in variables.items():
            rendered = rendered.replace(f"{{{{{key}}}}}", str(value))
        return rendered


@lru_cache(maxsize=1)
def get_prompt_registry() -> PromptRegistry:
    return PromptRegistry()
