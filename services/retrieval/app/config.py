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
    """检索服务配置。

    继承三套配置（嵌入/向量库/索引库），各自负责连接与超时；本类只定义检索策略与安全开关。
    配置分两层：**env**（连接串、端口、``allow_unfiltered_search`` 等环境/安全项）
    与 **configs/retrievers/*.yaml**（权重、条数、阈值等策略微调，文件优先、需重启生效）。
    白名单外的 yaml 键一律忽略（见 ``TUNABLE_KEYS``）。
    """

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
    # 必须是**中文可用**的交叉编码器：默认曾用 Xenova/ms-marco-MiniLM-L-6-v2，那是英文
    # MS MARCO 训练出来的，对中文语料的排序能力不可用（同类错误：BGE 指令只能加在查询侧）。
    # bge-reranker-base 与嵌入模型（bge-small-zh-v1.5）同族，约 1GB，首次使用会下载。
    rerank_model: str = "BAAI/bge-reranker-base"
    # 开启重排时，/search 返回的候选条数（>top_k）。
    # 为什么需要它：融合结果若先截断到 top_k，重排就只能对这几条**改顺序**，
    # 无法把第 6~20 名里真正相关的文档捞回 top_k —— 那样重排等于白开。
    # 调用方（编排层 rerank 节点）负责重排后再截断到 top_k。
    rerank_candidates: int = 20

    # ---- 权限（安全开关，刻意不放进 yaml 白名单：安全开关应由 .env 显式表达）----
    # 允许**不带 ACL** 的全库检索。默认关闭即 fail-closed：
    # 请求缺 ACL 时直接拒绝，而不是把全部租户的文档当检索结果返回。
    # 只应在本机调试检索服务时临时打开，生产必须保持 false。
    allow_unfiltered_search: bool = False

    # ---- 调参文件（可选）----
    retriever_config_path: str = "configs/retrievers/default.yaml"

    def with_overrides(self) -> Settings:
        """把调参文件的值覆盖到 ``.env`` 之上（**文件优先**）。

        两个必须知道的前提：
            1. **改调参文件必须重启服务**——本方法只在服务构造时调用一次，
               不是热加载。文件名里的 "default" 是单一全局配置，没有按环境/域选择机制；
            2. **方向是文件覆盖 env**，不是 env 覆盖文件。所以调参只能改文件，
               设同名环境变量不会生效。

        这样分工是刻意的：env 表达环境差异（连接串、开关），文件表达策略微调
        （权重、条数、阈值），两者职责不重叠。
        白名单外的键（含连接串与口令）一律忽略，见 :func:`load_retriever_overrides`。
        """
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
