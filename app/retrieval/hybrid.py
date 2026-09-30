"""混合检索入口：稠密 + 稀疏 → RRF 融合 → 可选重排，含降级。

对应 `开发流程/04-检索与编排设计.md` §2.1 与 §2.6~§2.9。

降级矩阵：

| 稠密通道 | 稀疏通道 | 模式 | 行为 |
| --- | --- | --- | --- |
| ✅ | ✅ | `hybrid` | 两路召回 → RRF 融合 →（可选）重排 |
| ✅ | ❌ | `dense_only` | 只用向量结果 |
| ❌ | ✅ | `sparse_only` | 只用 BM25 结果 |
| ❌ | ❌ | `degraded` | 返回空结果，交由上层转人工 |

两路**并行执行**并各自带超时：单路超时/报错只降级该路，不影响整体可用性。
"""

import hashlib
import re
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError

from app.core.config import get_settings
from app.core.logging import get_logger
from app.retrieval import dense, rerank, sparse
from app.retrieval.fuse import reciprocal_rank_fusion
from app.schemas.retrieval import Chunk, RetrievalFilters, RetrievalMode

logger = get_logger(__name__)

#: 近重复判定用的前缀长度（同一段内容被不同来源收录时前缀高度一致）
_NEAR_DUP_PREFIX = 120


def _chunk_keys(chunk: Chunk) -> tuple[str, str]:
    """返回 (精确键, 近重复键)。"""
    exact = chunk.content_hash or hashlib.sha256(
        re.sub(r"\s+", "", chunk.content).encode("utf-8")
    ).hexdigest()
    near = re.sub(r"\s+", "", chunk.content)[:_NEAR_DUP_PREFIX]
    return exact, near


def dedupe_chunks(chunks: list[Chunk]) -> list[Chunk]:
    """查询期去重。

    近重复片段（同一内容被多个来源收录、或切分重叠产生的副本）会占满 Top-K，
    把其他真正相关的内容挤出去，因此融合后必须先去掉它们。
    """
    seen_exact: set[str] = set()
    seen_near: set[str] = set()
    result: list[Chunk] = []

    for chunk in chunks:
        exact, near = _chunk_keys(chunk)
        if exact in seen_exact or near in seen_near:
            continue
        seen_exact.add(exact)
        seen_near.add(near)
        result.append(chunk)
    return result


def _collect(future, timeout: float, channel: str) -> tuple[list[Chunk], str | None]:
    """收集单通道结果；超时或异常都降级为「空结果 + 错误说明」。

    注意：`future.cancel()` 只能取消**尚未开始**的任务；已在执行的线程无法被强制中断，
    这里选择「不再等待」，让它自行结束，避免拖住整体响应。
    """
    try:
        return future.result(timeout=timeout), None
    except FuturesTimeoutError:
        future.cancel()
        logger.warning("%s 通道超时（%.1fs），该通道降级", channel, timeout)
        return [], "timeout"
    except Exception as exc:  # noqa: BLE001 - 降级是刻意设计，需吞掉所有通道异常
        logger.warning("%s 通道失败，该通道降级: %s", channel, exc)
        return [], f"{type(exc).__name__}: {exc}"


def _resolve_mode(dense_error: str | None, sparse_error: str | None) -> RetrievalMode:
    if dense_error and sparse_error:
        return RetrievalMode.DEGRADED
    if dense_error:
        return RetrievalMode.SPARSE_ONLY
    if sparse_error:
        return RetrievalMode.DENSE_ONLY
    return RetrievalMode.HYBRID


def _build_debug(
    mode: RetrievalMode,
    dense_chunks: list[Chunk],
    sparse_chunks: list[Chunk],
    fused: list[Chunk],
    reranked: bool,
    dense_error: str | None,
    sparse_error: str | None,
    final: list[Chunk],
) -> dict:
    return {
        "mode": mode.value,
        "dense_hits": len(dense_chunks),
        "sparse_hits": len(sparse_chunks),
        "fused": len(fused),
        "reranked": reranked,
        "dense_error": dense_error,
        "sparse_error": sparse_error,
        "top": [
            {
                "chunk_id": c.chunk_id,
                "rank": c.rank,
                "rrf_score": c.rrf_score,
                "dense_score": c.dense_score,
                "sparse_score": c.sparse_score,
                "rerank_score": c.rerank_score,
            }
            for c in final
        ],
    }


def hybrid_search(
    query: str,
    top_k: int | None = None,
    top_n_dense: int | None = None,
    top_n_sparse: int | None = None,
    top_n_fused: int | None = None,
    rerank_enabled: bool | None = None,
    filters: RetrievalFilters | None = None,
) -> tuple[list[Chunk], RetrievalMode, dict]:
    """执行混合检索。

    :return: `(Top-K 片段, 实际生效模式, 调试图信息)`
    """
    settings = get_settings()
    top_k = top_k if top_k is not None else settings.top_k
    top_n_dense = top_n_dense if top_n_dense is not None else settings.top_n_dense
    top_n_sparse = top_n_sparse if top_n_sparse is not None else settings.top_n_sparse
    top_n_fused = top_n_fused if top_n_fused is not None else settings.top_n_fused
    use_rerank = settings.rerank_enabled if rerank_enabled is None else rerank_enabled
    timeout = settings.channel_timeout_seconds

    # 1) 两路并行召回（各自独立降级）
    with ThreadPoolExecutor(max_workers=2) as pool:
        dense_future = pool.submit(dense.search_dense, query, top_n_dense, filters)
        sparse_future = pool.submit(sparse.search_sparse, query, top_n_sparse, filters)
        dense_chunks, dense_error = _collect(dense_future, timeout, "dense")
        sparse_chunks, sparse_error = _collect(sparse_future, timeout, "sparse")

    mode = _resolve_mode(dense_error, sparse_error)

    if mode is RetrievalMode.DEGRADED:
        logger.warning("两路通道均不可用，检索降级为空结果（建议转人工）")
        return [], mode, _build_debug(mode, [], [], [], False, dense_error, sparse_error, [])

    # 2) 融合
    if mode is RetrievalMode.HYBRID:
        fused = reciprocal_rank_fusion(
            [dense_chunks, sparse_chunks], k=settings.rrf_k, top_n=top_n_fused
        )
    elif mode is RetrievalMode.DENSE_ONLY:
        fused = dense_chunks[:top_n_fused]
    else:  # SPARSE_ONLY
        fused = sparse_chunks[:top_n_fused]

    # 3) 去重（近重复片段会占满 Top-K，挤掉其他相关内容）
    fused_raw = fused
    fused = dedupe_chunks(fused)
    if len(fused) < len(fused_raw):
        logger.info("融合后去重: %d → %d", len(fused_raw), len(fused))

    # 4) 可选重排（失败/未实现则跳过，不阻塞主流程）
    reranked = False
    candidates = fused
    if use_rerank and fused:
        try:
            candidates = rerank.rerank(query, fused, top_k)
            reranked = True
        except NotImplementedError:
            logger.warning("重排尚未实现，本次跳过（RERANK_ENABLED=false 可避免该日志）")
        except Exception as exc:  # noqa: BLE001 - 重排是增强项，失败不应影响召回
            logger.warning("重排失败，本次跳过: %s", exc)

    # 5) 截断到 Top-K
    final = candidates[:top_k]
    debug = _build_debug(
        mode, dense_chunks, sparse_chunks, fused, reranked, dense_error, sparse_error, final
    )
    debug["fused_before_dedupe"] = len(fused_raw)

    logger.info(
        "混合检索完成: mode=%s dense=%d sparse=%d fused=%d reranked=%s final=%d",
        debug["mode"],
        debug["dense_hits"],
        debug["sparse_hits"],
        debug["fused"],
        debug["reranked"],
        len(final),
    )
    return final, mode, debug
