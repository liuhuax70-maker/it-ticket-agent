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
        """按 ``kind`` 分发到查询侧或文档侧。

        ⚠️ 未知 ``kind`` 会**静默落到文档侧**。这是有意的宽松（避免上游新增枚举值
        就整条链路报错），但代价必须知道：BGE 中文系列里"查询侧加指令、文档侧不加"，
        指令加错位置会让召回明显变差且不报任何错。所以新增 kind 时必须同步：
        上游 ``EmbedRequest.kind`` 的 Literal、以及这里的显式分支。
        """
        if kind == "query":
            return [await self.embed_query(t) for t in texts]
        return await self.embed_documents(texts)
