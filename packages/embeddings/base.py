"""向量化后端抽象。"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod


def l2_normalize(vector: list[float]) -> list[float]:
    """L2 归一化。COSINE 距离下不归一化会导致分数区间漂移、阈值无法设定。"""
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0.0:
        return vector
    return [v / norm for v in vector]


class Embedder(ABC):
    """统一向量化接口。文档侧与查询侧分开，避免指令加错位置。"""

    model_name: str = "unknown"
    dim: int = 0

    @abstractmethod
    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """文档侧：不加查询指令。"""

    @abstractmethod
    async def embed_query(self, text: str) -> list[float]:
        """查询侧：按模型要求加指令后向量化。"""

    async def embed(self, texts: list[str], kind: str = "document") -> list[list[float]]:
        if kind == "query":
            return [await self.embed_query(t) for t in texts]
        return await self.embed_documents(texts)
