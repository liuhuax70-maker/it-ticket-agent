"""重排：Qwen3-Reranker（可开关）。

MVP 默认关闭（配置项 RERANK_ENABLED），v1.1 启用；
超时则跳过重排，直接使用融合结果。
"""

from app.schemas.retrieval import Chunk


def rerank(query: str, chunks: list[Chunk], top_k: int) -> list[Chunk]:
    """对候选片段按相关性重排并取 Top-K，写入 rerank_score 与 rank。"""
    # TODO(后续)：调用 Qwen3-Reranker，并处理超时降级
    raise NotImplementedError("骨架占位：rerank 将在后续编码阶段实现")
