"""混合检索入口：编排稠密 + 稀疏 + RRF + 重排，并处理降级。

降级策略（对应 `开发流程/04-检索与编排设计.md` §2.9）：
- 向量服务不可用 → 仅 BM25，mode=sparse_only
- BM25 不可用     → 仅向量，mode=dense_only
- 重排超时/关闭   → 直接用融合结果
- 两路皆不可用   → mode=degraded，返回空结果
"""

from app.schemas.retrieval import Chunk, RetrievalFilters, RetrievalMode


def hybrid_search(
    query: str,
    top_k: int | None = None,
    top_n_dense: int | None = None,
    top_n_sparse: int | None = None,
    top_n_fused: int | None = None,
    rerank_enabled: bool | None = None,
    filters: RetrievalFilters | None = None,
) -> tuple[list[Chunk], RetrievalMode, dict]:
    """返回 (Top-K 片段, 实际检索模式, 调试图信息)。"""
    # TODO(后续)：串联 dense/sparse → fuse → rerank，并落实降级
    raise NotImplementedError("骨架占位：hybrid_search 将在后续编码阶段实现")
