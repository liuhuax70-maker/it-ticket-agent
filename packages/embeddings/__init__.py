"""向量化后端：local(fastembed/sentence-transformers) 与远端(litellm)。"""

from packages.embeddings.base import Embedder, l2_normalize
from packages.embeddings.config import EmbedSettings
from packages.embeddings.factory import get_embedder

__all__ = ["Embedder", "EmbedSettings", "get_embedder", "l2_normalize"]
