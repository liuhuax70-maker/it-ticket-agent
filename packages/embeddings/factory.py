"""向量化后端工厂（进程级单例，避免重复加载模型）。"""

from __future__ import annotations

from packages.common.errors import ConfigError
from packages.embeddings.base import Embedder
from packages.embeddings.config import EmbedSettings

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
    key = (settings.embed_backend.lower(), settings.embed_model)
    if key not in _instances:
        _instances[key] = _build(settings)
    return _instances[key]


def reset_embedder_cache() -> None:
    """测试用。"""
    _instances.clear()
