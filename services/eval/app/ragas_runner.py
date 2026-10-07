"""RAGAS 跑批。

两段式设计：
    1. **采集**：逐条调用 api-gateway 的 /chat（``include_contexts=true``），
       拿到「答案 + 完整上下文 + 引用」，落成中间结果；
    2. **打分**：把中间结果交给 RAGAS 计算指标。

这样即使裁判模型临时不可用，采集结果也不会白跑（可离线重打分）。

依赖：``pip install -e '.[eval]'``（ragas + datasets + langchain-openai）。
未安装时本模块给出明确指引，而不是抛 ImportError 堆栈。
"""

from __future__ import annotations

import asyncio
import time

import httpx

from app.config import Settings
from app.datasets import GoldenSample
from packages.common.errors import ConfigError, UpstreamError
from packages.common.logging import get_logger
from packages.contracts import ChatRequest
from packages.llms import build_target

logger = get_logger("eval.ragas")

REQUIRES_EMBEDDING = {"answer_relevancy", "answer_correctness"}


async def collect_answers(
    samples: list[GoldenSample], settings: Settings
) -> list[dict[str, object]]:
    """调用被测系统，采集 RAGAS 需要的四元组。"""
    rows: list[dict[str, object]] = []
    headers = {
        "x-tenant-id": settings.tenant_id,
        "x-department-id": settings.department_id,
        "x-user-id": settings.user_id,
    }
    async with httpx.AsyncClient(timeout=settings.request_timeout) as client:
        for index, sample in enumerate(samples, start=1):
            started = time.perf_counter()
            try:
                payload = ChatRequest(
                    query=sample.question, include_contexts=True
                ).model_dump(mode="json")
                resp = await client.post(
                    f"{settings.api_gateway_url.rstrip('/')}/chat",
                    json=payload,
                    headers=headers,
                )
                resp.raise_for_status()
                body = resp.json()
            except Exception as exc:  # noqa: BLE001
                logger.error("采集失败 sample=%s err=%s", sample.question[:40], exc)
                raise UpstreamError("api-gateway", f"采集第 {index} 条失败: {exc}") from exc

            contexts = [item["text"] for item in (body.get("contexts") or [])]
            rows.append(
                {
                    "user_input": sample.question,
                    "response": body.get("answer", ""),
                    "retrieved_contexts": contexts or [c.get("snippet", "") for c in body.get("citations", [])],
                    "reference": sample.reference,
                    "refused": bool(body.get("refused")),
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                    "tags": sample.tags,
                }
            )
            logger.info("[%s/%s] 采集完成: %s", index, len(samples), sample.question[:40])
    return rows


def _build_judge(settings: Settings):
    """构造 RAGAS 的裁判 LLM（复用 LiteLLM 的 provider 解析）。"""
    try:
        from langchain_openai import ChatOpenAI
        from ragas.llms import LangchainLLMWrapper
    except ImportError as exc:
        raise ConfigError(
            "评测依赖未安装。执行 `pip install -e '.[eval]'` 后再跑评测"
        ) from exc

    target = build_target(settings, settings.judge_model or None)
    base_url = target.api_base or (
        "https://api.deepseek.com/v1" if target.provider == "deepseek" else None
    )
    llm = ChatOpenAI(
        model=target.name,
        api_key=target.api_key or "not-needed",
        base_url=base_url,  # type: ignore[arg-type]
        temperature=0.0,
    )
    return LangchainLLMWrapper(llm), target.name


def score(rows: list[dict[str, object]], settings: Settings) -> dict[str, object]:
    """用 RAGAS 打分。需要 ``eval`` 可选依赖。"""
    try:
        from ragas import EvaluationDataset, evaluate
        from ragas.metrics import (
            Faithfulness,
            LLMContextPrecisionWithoutReference,
            LLMContextRecall,
        )
    except ImportError as exc:
        raise ConfigError(
            "未安装 ragas。执行 `pip install -e '.[eval]'` 后再跑评测"
        ) from exc

    metrics_map = {
        "faithfulness": Faithfulness,
        "context_precision": LLMContextPrecisionWithoutReference,
        "context_recall": LLMContextRecall,
    }
    requested = settings.metric_list()
    unsupported = [m for m in requested if m in REQUIRES_EMBEDDING]
    if unsupported:
        logger.warning(
            "以下指标需要 embedding 模型，当前跳过: %s（配置 EMBED_* 后可启用）", unsupported
        )
    selected = [m for m in requested if m in metrics_map]
    if not selected:
        raise ConfigError(f"没有可用指标（可选: {sorted(metrics_map)}；请求: {requested}）")

    llm, judge_name = _build_judge(settings)
    dataset = EvaluationDataset.from_list(
        [
            {
                "user_input": row["user_input"],
                "response": row["response"],
                "retrieved_contexts": row["retrieved_contexts"],
                "reference": row["reference"] or "",
            }
            for row in rows
        ]
    )
    logger.info("开始 RAGAS 打分: 样本 %s 条，指标 %s，裁判 %s", len(rows), selected, judge_name)

    result = evaluate(
        dataset=dataset,
        metrics=[metrics_map[m]() for m in selected],
        llm=llm,
        raise_exceptions=False,
    )
    scores = {k: (float(v) if isinstance(v, (int, float)) else None) for k, v in dict(result).items()}
    return {"metrics": scores, "judge_model": judge_name, "requested_metrics": requested}


async def run(samples: list[GoldenSample], settings: Settings) -> dict[str, object]:
    rows = await collect_answers(samples, settings)
    # RAGAS 自身是同步阻塞的，放到线程里避免卡住事件循环
    scoring = await asyncio.to_thread(score, rows, settings)
    refused = sum(1 for row in rows if row["refused"])
    latencies = [float(row["latency_ms"]) for row in rows]
    return {
        **scoring,
        "count": len(rows),
        "refused_count": refused,
        "refusal_rate": round(refused / len(rows), 4) if rows else 0.0,
        "latency_ms_avg": round(sum(latencies) / len(latencies), 1) if latencies else 0.0,
        "per_sample": rows,
    }
