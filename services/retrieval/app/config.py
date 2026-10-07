"""retrieval 配置。"""

from __future__ import annotations

from pathlib import Path

import yaml

from packages.common.constants import SERVICE_RETRIEVAL
from packages.common.logging import get_logger
from packages.contracts import RetrieveMode
from packages.embeddings.config import EmbedSettings
from packages.search.config import OpenSearchSettings
from packages.vectorstores.config import MilvusSettings

logger = get_logger("retrieval.config")

# 允许被 configs/retrievers/*.yaml 覆盖的检索参数。
# 白名单而非全量覆盖：连接串、端口这类东西不该由检索调参文件决定。
TUNABLE_KEYS = {
    "retrieve_mode",
    "top_k",
    "vector_top_k",
    "bm25_top_k",
    "rrf_k",
    "rrf_weight_vector",
    "rrf_weight_bm25",
    "min_score",
    "rerank_enabled",
    "rerank_model",
}


def load_retriever_overrides(path: str | Path) -> dict:
    """读取检索调参文件；不存在或格式非法时返回空字典（不让服务起不来）。"""
    file = Path(path)
    if not file.exists():
        logger.info("未找到检索调参文件 %s，使用 .env 默认值", file)
        return {}
    try:
        data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("解析 %s 失败，使用 .env 默认值: %s", file, exc)
        return {}

    # 允许 {retrieval: {...}} 或平铺两种写法
    section = data.get("retrieval", data) if isinstance(data, dict) else {}
    unknown = set(section) - TUNABLE_KEYS
    if unknown:
        logger.warning("忽略不可调的检索参数: %s（可调项见 TUNABLE_KEYS）", sorted(unknown))
    return {k: v for k, v in section.items() if k in TUNABLE_KEYS}


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

    # ---- 调参文件（可选）----
    retriever_config_path: str = "configs/retrievers/default.yaml"

    def with_overrides(self) -> Settings:
        """把调参文件的值叠加到 .env 之上（文件优先，便于不重启环境变量做灰度调参）。"""
        overrides = load_retriever_overrides(self.retriever_config_path)
        if not overrides:
            return self
        logger.info("应用检索调参: %s", overrides)
        return self.model_copy(update=overrides)

    def default_mode(self) -> RetrieveMode:
        try:
            return RetrieveMode(self.retrieve_mode)
        except ValueError:
            return RetrieveMode.hybrid
