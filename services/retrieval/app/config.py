"""retrieval 配置。"""

from __future__ import annotations

from packages.common.constants import SERVICE_RETRIEVAL
from packages.contracts import RetrieveMode
from packages.embeddings.config import EmbedSettings
from packages.search.config import OpenSearchSettings
from packages.vectorstores.config import MilvusSettings


class Settings(EmbedSettings, MilvusSettings, OpenSearchSettings):
    service_name: str = SERVICE_RETRIEVAL
    host: str = "0.0.0.0"
    port: int = 8002

    # ---- 检索 ----
    retrieve_mode: str = RetrieveMode.hybrid.value  # vector | keyword | hybrid
    top_k: int = 5
    vector_top_k: int = 20
    bm25_top_k: int = 20
    rrf_k: int = 60
    # RRF 各路权重（BM25 在短查询上更稳，向量在语义改写上更强）
    rrf_weight_vector: float = 1.0
    rrf_weight_bm25: float = 1.0
    # 低于该分数的结果直接丢弃；0 表示不过滤
    min_score: float = 0.0

    # ---- 重排 ----
    rerank_enabled: bool = False
    rerank_model: str = "Xenova/ms-marco-MiniLM-L-6-v2"

    def default_mode(self) -> RetrieveMode:
        try:
            return RetrieveMode(self.retrieve_mode)
        except ValueError:
            return RetrieveMode.hybrid
