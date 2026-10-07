"""远端向量化后端（LiteLLM，OpenAI 兼容 /v1/embeddings）。"""

from __future__ import annotations

from packages.common.errors import UpstreamError
from packages.embeddings.base import Embedder, l2_normalize


class LiteLLMEmbedder(Embedder):
    def __init__(
        self,
        model_name: str,
        *,
        api_base: str = "",
        api_key: str = "",
        dim: int = 512,
        batch_size: int = 32,
    ) -> None:
        self.model_name = model_name
        self.dim = dim
        self._batch_size = batch_size
        self._api_base = api_base or None
        self._api_key = api_key or None
        # 无 provider 前缀时按 OpenAI 兼容处理
        self._litellm_model = model_name if "/" in model_name else f"openai/{model_name}"

    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        import litellm

        kwargs: dict = {"model": self._litellm_model, "input": texts}
        if self._api_base:
            kwargs["api_base"] = self._api_base
        if self._api_key:
            kwargs["api_key"] = self._api_key
        try:
            resp = await litellm.aembedding(**kwargs)
        except Exception as exc:  # noqa: BLE001
            raise UpstreamError("embeddings", f"{self.model_name} 向量化失败: {exc}") from exc
        return [l2_normalize([float(x) for x in item["embedding"]]) for item in resp.data]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        results: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            results.extend(await self._embed_batch(batch))
        return results

    async def embed_query(self, text: str) -> list[float]:
        vectors = await self._embed_batch([text])
        return vectors[0]
