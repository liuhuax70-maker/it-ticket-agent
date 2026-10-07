"""本地 sentence-transformers 后端（可选，体积大）。"""

from __future__ import annotations

import asyncio

from packages.common.errors import ConfigError
from packages.embeddings.base import Embedder, l2_normalize


class SentenceTransformerEmbedder(Embedder):
    def __init__(
        self,
        model_name: str,
        *,
        query_instruction: str = "",
        batch_size: int = 32,
    ) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover
            raise ConfigError(
                "未安装 sentence-transformers。执行 `pip install -e '.[local-embed]'`，"
                "或改用 EMBED_BACKEND=fastembed"
            ) from exc

        self.model_name = model_name
        self._instruction = query_instruction
        self._batch_size = batch_size
        self._model = SentenceTransformer(model_name)
        self.dim = int(self._model.get_sentence_embedding_dimension() or 0)

    def _encode(self, texts: list[str]) -> list[list[float]]:
        vectors = self._model.encode(
            texts, batch_size=self._batch_size, normalize_embeddings=True, show_progress_bar=False
        )
        return [l2_normalize([float(x) for x in vec]) for vec in vectors]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return await asyncio.to_thread(self._encode, texts)

    async def embed_query(self, text: str) -> list[float]:
        payload = f"{self._instruction}{text}" if self._instruction else text
        vectors = await asyncio.to_thread(self._encode, [payload])
        return vectors[0]
