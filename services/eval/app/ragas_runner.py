"""RAGAS 打分（L2）：答案质量这类需要主观判断的维度。

分工：L1（``metrics.py``）负责可判定的硬事实（命中/拒答/越权），
L2 负责"答案是否忠于上下文、是否切题"。两层都要看：
只修 L1 会得到一堆引用正确但答不对题的答案，只看 L2 会漏掉越权。

裁判模型：用 ``app.judge.ProjectLLMJudge``（复用项目的 LLMClient），
不直接用 ragas 的 langchain/OpenAI 包装——原因见 judge.py 模块注释
（本地思考型模型在 OpenAI 兼容协议下关不掉思考链，会拿到空输出）。

指标口径：**负样本（应拒答）不参与 L2**。拒答文本与上下文无关是应该的，
拿它算 faithfulness 只会得到无意义的低分，把真实信号淹没掉。
"""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING, Any

from app.config import Settings
from packages.common.errors import ConfigError
from packages.common.logging import get_logger

if TYPE_CHECKING:  # 只在类型检查时引入：运行时不装 eval extra 也能 import 本模块
    from app.judge import ProjectLLMJudge

logger = get_logger("eval.ragas")

# 需要 embedding 模型的指标：当前不启用（本地 embedding 未接入 RAGAS 的 wrapper 体系）
REQUIRES_EMBEDDING = {"answer_relevancy", "answer_correctness", "semantic_similarity", "answer_similarity"}

# 指标键 -> ragas 指标类名。
# 这里用 ragas 的**经典指标**（ragas.metrics）而不是 collections：
# collections 只接受 instructor 结构化输出的 InstructorLLM，
# 而我们的裁判是文本型 BaseRagasLLM（为了能关掉本地模型的思考链）。
# 依赖版本已锁在 pyproject 的 eval extra 里，升级 ragas 时需要一并迁移到 collections。
LEGACY_METRIC_NAMES = {
    "faithfulness": "Faithfulness",
    "context_precision": "LLMContextPrecisionWithoutReference",
    "context_recall": "LLMContextRecall",
}


def _load_metrics(selected: list[str], judge: ProjectLLMJudge) -> list[tuple[str, Any]]:
    try:
        import ragas.metrics as ragas_metrics
    except ImportError as exc:
        raise ConfigError("未安装 ragas。执行 `pip install -e '.[eval]'` 后再跑评测") from exc

    loaded: list[tuple[str, Any]] = []
    with warnings.catch_warnings():
        # 经典指标在 0.4 里会打 DeprecationWarning，这里是有意选择，不必刷屏
        warnings.simplefilter("ignore", DeprecationWarning)
        for key in selected:
            class_name = LEGACY_METRIC_NAMES[key]
            factory = getattr(ragas_metrics, class_name)
            loaded.append((key, factory(llm=judge)))
    return loaded


def _to_ragas_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把采集行转成 RAGAS 需要的字段（跳过负样本与失败样本）。"""
    converted: list[dict[str, Any]] = []
    for row in rows:
        if row.get("should_refuse") or row.get("error") or not row.get("answer"):
            continue
        converted.append(
            {
                "user_input": row["question"],
                "response": row.get("answer", ""),
                "retrieved_contexts": list(row.get("contexts") or []) or [""],
                "reference": row.get("reference") or row.get("answer", ""),
            }
        )
    return converted


def score(rows: list[dict[str, Any]], settings: Settings) -> dict[str, Any]:
    """对采集结果做 RAGAS 打分。需要 ``eval`` 可选依赖。"""
    try:
        from ragas import EvaluationDataset, evaluate
    except ImportError as exc:
        raise ConfigError("未安装 ragas。执行 `pip install -e '.[eval]'` 后再跑评测") from exc

    from app.judge import ProjectLLMJudge

    requested = settings.metric_list()
    skipped = [m for m in requested if m in REQUIRES_EMBEDDING]
    if skipped:
        logger.warning("以下指标需要 embedding 模型，当前跳过: %s", skipped)
    selected = [m for m in requested if m in LEGACY_METRIC_NAMES]
    if not selected:
        raise ConfigError(
            f"没有可用指标（可选: {sorted(LEGACY_METRIC_NAMES)}；请求: {requested}）"
        )

    ragas_rows = _to_ragas_rows(rows)
    if not ragas_rows:
        return {"metrics": {}, "judge_model": None, "scored_count": 0, "note": "没有可用于 L2 的样本"}

    judge = ProjectLLMJudge(settings)
    metrics = _load_metrics(selected, judge)
    logger.info(
        "开始 RAGAS 打分: %s 条，指标 %s，裁判 %s（超时 %ss，并发 %s）",
        len(ragas_rows),
        selected,
        judge.target_name,
        settings.judge_timeout,
        settings.judge_workers,
    )

    from ragas.run_config import RunConfig

    run_config = RunConfig(timeout=settings.judge_timeout, max_workers=settings.judge_workers)
    result = evaluate(
        dataset=EvaluationDataset.from_list(ragas_rows),
        metrics=[metric for _, metric in metrics],
        raise_exceptions=False,
        run_config=run_config,
    )
    frame = result.to_pandas()

    scores: dict[str, float | None] = {}
    per_sample: dict[str, list[float | None]] = {}
    for key, metric in metrics:
        column = getattr(metric, "name", key)
        if column not in frame.columns:
            logger.warning("结果里没有指标列 %s（实际列: %s）", column, list(frame.columns))
            scores[key] = None
            continue
        series = frame[column].astype("float64")
        scores[key] = None if series.isna().all() else round(float(series.mean()), 4)
        per_sample[key] = [None if v != v else round(float(v), 4) for v in series.tolist()]

    nan_metrics = [k for k, v in scores.items() if v is None]
    if nan_metrics:
        logger.warning("以下指标全部为 NaN（裁判输出无法解析）: %s", nan_metrics)

    return {
        "metrics": scores,
        "per_sample": per_sample,
        "judge_model": judge.target_name,
        "judge_usage": judge.usage,
        "requested_metrics": requested,
        "scored_count": len(ragas_rows),
    }


__all__ = ["LEGACY_METRIC_NAMES", "REQUIRES_EMBEDDING", "score"]
