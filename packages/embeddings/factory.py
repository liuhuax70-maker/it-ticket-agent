"""向量化后端工厂（进程级单例，避免重复加载模型）。"""

from __future__ import annotations

from packages.common.errors import ConfigError
from packages.embeddings.base import Embedder
from packages.embeddings.config import EmbedSettings

# 单例缓存。键必须覆盖**所有影响向量数值或行为的字段**。
# 当前键只有 (backend, model)：改了 embed_query_instruction / embed_dim /
# embed_api_base / embed_batch_size 都拿不到新实例——表现为"配置改了但没效果"，
# 且不报任何错（这是配置类问题里最难查的一种）。补键时请一并更新 reset_embedder_cache
# 的调用方（测试需要在切换配置后调用它）。
_instances: dict[tuple[str, str], Embedder] = {}


def _build(settings: EmbedSettings) -> Embedder:
    backend = (settings.embed_backend or "fastembed").lower()

    if backend == "fastembed":
        from packages.embeddings.fastembed_backend import FastEmbedEmbedder

        return FastEmbedEmbedder(
            settings.embed_model,
            query_instruction=settings.embed_query_instruction,
            batch_size=settings.embed_batch_size,
            expected_dim=settings.embed_dim,
        )

    if backend == "sentence_transformers":
        from packages.embeddings.st_backend import SentenceTransformerEmbedder

        return SentenceTransformerEmbedder(
            settings.embed_model,
            query_instruction=settings.embed_query_instruction,
            batch_size=settings.embed_batch_size,
        )

    if backend == "litellm":
        from packages.embeddings.litellm_backend import LiteLLMEmbedder

        return LiteLLMEmbedder(
            settings.embed_model,
            api_base=settings.embed_api_base,
            api_key=settings.embed_api_key,
            dim=settings.embed_dim,
            batch_size=settings.embed_batch_size,
        )

    raise ConfigError(
        f"不支持的 EMBED_BACKEND={backend!r}（可选：fastembed | litellm | sentence_transformers）"
    )


def get_embedder(settings: EmbedSettings) -> Embedder:
    """进程级单例获取向量化后端。

    单例键为 ``(backend, model)``（见模块顶部注释）：改 ``query_instruction`` / ``dim``
    等其它字段不会生成新实例，表现为「配置改了没效果」且不报错；切换需在测试里
    ``reset_embedder_cache()``。
    """
    key = (settings.embed_backend.lower(), settings.embed_model)
    if key not in _instances:
        _instances[key] = _build(settings)
    return _instances[key]


def reset_embedder_cache() -> None:
    """测试用。"""
    _instances.clear()
