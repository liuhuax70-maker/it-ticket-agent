"""本地 ONNX 向量化后端（fastembed）。

选它而不是 sentence-transformers：体积小一个数量级（无 torch），
足以支撑最小闭环；需要更强模型时切 ``sentence_transformers`` 或远端 litellm。
"""

from __future__ import annotations

import asyncio

from packages.common.errors import ConfigError
from packages.common.logging import get_logger
from packages.embeddings.base import Embedder, l2_normalize

logger = get_logger("embeddings.fastembed")


class FastEmbedEmbedder(Embedder):
    def __init__(
        self,
        model_name: str,
        *,
        query_instruction: str = "",
        batch_size: int = 32,
        expected_dim: int = 0,
    ) -> None:
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:  # pragma: no cover
            raise ConfigError(
                "未安装 fastembed。执行 `pip install fastembed`，"
                "或把 EMBED_BACKEND 切换为 litellm / sentence_transformers"
            ) from exc

        self.model_name = model_name
        self._instruction = query_instruction
        self._batch_size = batch_size

        try:
            self._model = TextEmbedding(model_name=model_name)
        except Exception as exc:  # noqa: BLE001
            raise ConfigError(
                f"加载 fastembed 模型 {model_name!r} 失败: {exc}。"
                "请确认模型名在 fastembed 支持列表内，或改用 EMBED_BACKEND=litellm"
            ) from exc

        probe = next(iter(self._model.embed(["向量维度探测"])))
        self.dim = int(len(probe))
        if expected_dim and expected_dim != self.dim:
            logger.warning(
                "EMBED_DIM=%s 与模型实际维度 %s 不一致，已按模型维度运行；"
                "若向量库已有数据必须先删 collection 再重建",
                expected_dim,
                self.dim,
            )

    def _embed_sync(self, texts: list[str], *, is_query: bool) -> list[list[float]]:
        if is_query and self._instruction:
            texts = [self._instruction + t for t in texts]
        vectors = self._model.embed(texts, batch_size=self._batch_size)
        return [l2_normalize([float(x) for x in vec]) for vec in vectors]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return await asyncio.to_thread(self._embed_sync, texts, is_query=False)

    async def embed_query(self, text: str) -> list[float]:
        vectors = await asyncio.to_thread(self._embed_sync, [text], is_query=True)
        return vectors[0]
