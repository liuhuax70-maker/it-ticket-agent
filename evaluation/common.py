"""评估公共逻辑：加载评估集 + 运行「被测系统」。

被测系统就是项目自身的检索 + 生成链路：
    hybrid_search()  →  RAG 上下文
    generate()       →  回答

因此评估结果直接反映线上链路的效果。
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from app.core.config import get_settings
from app.core.logging import get_logger
from app.generation.ollama import generate
from app.generation.prompts import SYSTEM_PROMPT, USER_PROMPT_TEMPLATE, build_context
from app.retrieval.hybrid import hybrid_search

logger = get_logger(__name__)

TESTSET_PATH = Path(__file__).parent / "testset.jsonl"
REPORT_DIR = Path(__file__).parent / "reports"


@dataclass
class EvalItem:
    """一条评估样本。"""

    id: str
    type: str  # lexical（专有名词/错误码/版本号） | semantic（语义化提问）
    question: str
    reference_answer: str
    expected_chunk_ids: list[str] = field(default_factory=list)


@dataclass
class EvalRun:
    """被测系统在一条样本上的实际表现。"""

    item: EvalItem
    retrieved_ids: list[str]
    contexts: list[str]
    mode: str
    answer: str
    debug: dict


def load_testset(path: Path | str = TESTSET_PATH) -> list[EvalItem]:
    """读取 JSONL 评估集。"""
    path = Path(path)
    items: list[EvalItem] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            items.append(EvalItem(**json.loads(line)))
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("%s 第 %d 行解析失败，已跳过: %s", path.name, lineno, exc)
    logger.info("加载评估集 %s：%d 条", path.name, len(items))
    return items


def run_system(
    item: EvalItem,
    *,
    top_k: int | None = None,
    generate_answer: bool = True,
    rerank: bool | None = None,
) -> EvalRun:
    """跑一遍被测系统。"""
    chunks, mode, debug = hybrid_search(item.question, top_k=top_k, rerank_enabled=rerank)

    answer = ""
    if generate_answer and chunks:
        context = build_context(chunks)
        answer = generate(
            SYSTEM_PROMPT,
            USER_PROMPT_TEMPLATE.format(context=context, query=item.question),
        )

    return EvalRun(
        item=item,
        retrieved_ids=[c.chunk_id for c in chunks],
        contexts=[c.content for c in chunks],
        mode=mode.value,
        answer=answer,
        debug=debug,
    )


def ensure_report_dir() -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    return REPORT_DIR


def eval_config_snapshot() -> dict:
    """记录影响评估结果的关键配置，保证报告可复现。"""
    settings = get_settings()
    return {
        "milvus_collection": settings.milvus_collection,
        "embedding_model": settings.embedding_model,
        "llm_model": settings.llm_model,
        "top_k": settings.top_k,
        "top_n_dense": settings.top_n_dense,
        "top_n_sparse": settings.top_n_sparse,
        "top_n_fused": settings.top_n_fused,
        "rrf_k": settings.rrf_k,
        "rerank_enabled": settings.rerank_enabled,
    }
