"""重排：Qwen3-Reranker（本地 Ollama）。

Ollama 没有原生 rerank 接口，因此采用**生成式打分**：
把「查询 + 候选文档」交给模型，让它只输出 0~100 的整数相关性分数。

实测（`dengcao/Qwen3-Reranker-4B:Q5_K_M`，本机）：

| 文档 | 输出 |
| --- | --- |
| 相关（ERR-4041 令牌过期） | `100` |
| 无关（浏览器支持） | `0` |

调用要点：
- 必须传 `"think": false`，否则模型的 `<think>` 会占满输出导致结果为空；
- 单次约 1s，因此**并发打分**并**限制候选数**，避免总延迟失控。

降级（对应 `开发流程/04` §2.9）：单条失败则置于末尾；整体失败过半则抛错，
由 `hybrid_search` 捕获后跳过重排，沿用融合结果。
"""

import re
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger
from app.schemas.retrieval import Chunk

logger = get_logger(__name__)

#: 只匹配独立的三位以内整数。
#: 前后不允许是字母/下划线/数字/小数点，避免把 "2024年"、"v1.2.3" 里的数字误判为分数。
_SCORE_RE = re.compile(r"(?<![\w.])(\d{1,3})(?![\d.])")

#: 解析前先去掉的尾部标点，兼容 "85." / "85。" 这类写法
_TRAILING_PUNCT = "。．.！!？?；;，,、 " + "\n\t"

#: 单条文档送入模型的字符上限（控制 prompt 体积与耗时）
MAX_DOCUMENT_CHARS = 1500

PROMPT_TEMPLATE = (
    "判断下面「文档」与「查询」的相关性，只输出一个 0 到 100 的整数，不要输出任何解释。\n"
    "查询：{query}\n"
    "文档：{document}\n"
    "相关性分数："
)


@lru_cache
def _endpoint() -> str:
    return f"{get_settings().ollama_base_url.rstrip('/')}/api/generate"


def _parse_score(text: str) -> int | None:
    """从模型输出里解析 0~100 的整数分数；解析不到返回 None。"""
    if not text:
        return None
    cleaned = text.strip().rstrip(_TRAILING_PUNCT)
    match = _SCORE_RE.search(cleaned)
    if not match:
        return None
    return max(0, min(100, int(match.group(1))))


def _build_prompt(query: str, document: str) -> str:
    return PROMPT_TEMPLATE.format(query=query, document=document[:MAX_DOCUMENT_CHARS])


def _score_one(query: str, chunk: Chunk, timeout: float) -> int:
    """给单个候选打相关性分数（0~100）。失败抛异常，由调用方决定降级。"""
    settings = get_settings()
    payload = {
        "model": settings.reranker_model,
        "prompt": _build_prompt(query, chunk.content),
        "stream": False,
        "think": False,  # 关键：不关掉 thinking 会拿到空响应
        "options": {"temperature": 0, "num_predict": 8},
    }

    response = httpx.post(_endpoint(), json=payload, timeout=timeout)
    response.raise_for_status()
    raw_text = response.json().get("response", "")

    score = _parse_score(raw_text)
    if score is None:
        raise ValueError(f"无法解析重排分数: {raw_text[:60]!r}")
    return score


def rerank(
    query: str,
    chunks: list[Chunk],
    top_k: int,
    *,
    candidates: int | None = None,
    max_workers: int | None = None,
    timeout: float | None = None,
) -> list[Chunk]:
    """按相关性重排候选片段并取 Top-K。

    :param candidates: 参与重排的候选上限（其余按原顺序附加在末尾）。
    :raises RuntimeError: 失败条数过半时（调用方应跳过重排）。
    :return: 重排后的片段列表；`rerank_score` 归一化到 0~1。
    """
    if not chunks:
        return []

    settings = get_settings()
    limit = candidates if candidates is not None else settings.rerank_top_n
    workers = max_workers if max_workers is not None else settings.rerank_max_workers
    per_doc_timeout = timeout if timeout is not None else settings.rerank_timeout_seconds

    head = chunks[:limit]
    tail = chunks[limit:]

    scores: list[int | None] = [None] * len(head)
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(head)))) as pool:
        future_to_index = {
            pool.submit(_score_one, query, chunk, per_doc_timeout): index
            for index, chunk in enumerate(head)
        }
        for future, index in future_to_index.items():
            try:
                scores[index] = future.result()
            except Exception as exc:  # noqa: BLE001 - 单条失败不致命，记录后降级
                logger.warning("重排单条失败（%s）: %s", head[index].chunk_id, exc)

    failed = sum(1 for score in scores if score is None)
    if failed * 2 > len(head):
        raise RuntimeError(f"重排失败过多（{failed}/{len(head)}），放弃重排")

    # 有分数的按分数降序；无分数的排到最后且保持原有相对顺序（sorted 稳定）
    order = sorted(range(len(head)), key=lambda i: (scores[i] is None, -(scores[i] or 0)))

    result: list[Chunk] = []
    for rank, index in enumerate(order, start=1):
        score = scores[index]
        result.append(
            head[index].model_copy(
                update={
                    "rerank_score": None if score is None else round(score / 100.0, 4),
                    "rank": rank,
                }
            )
        )

    for offset, chunk in enumerate(tail, start=len(result) + 1):
        result.append(chunk.model_copy(update={"rerank_score": None, "rank": offset}))

    logger.info(
        "重排完成: 候选=%d 实际打分=%d 失败=%d Top-K=%d",
        len(head),
        len(head) - failed,
        failed,
        min(top_k, len(result)),
    )
    return result[:top_k] if top_k else result
