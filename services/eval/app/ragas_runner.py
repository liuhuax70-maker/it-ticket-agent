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
REQUIRES_EMBEDDING = {
    "answer_relevancy",
    "answer_correctness",
    "semantic_similarity",
    "answer_similarity",
}

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


def _aggregate(frame: Any, metrics: list[tuple[str, Any]]) -> tuple[dict, dict]:
    """从结果表里取每个指标的均值与逐样本分数。

    列名以 ragas 指标对象自带的 ``name`` 为准——本仓库里 context_precision 的真实列名是
    ``llm_context_precision_without_reference``，与我们对外的 key 不同。
    全部 NaN 时给出 ``None`` 并告警，不让 null 悄悄躺在报告里。
    """
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
        per_sample[key] = [
            None if value != value else round(float(value), 4) for value in series.tolist()
        ]

    unresolved = [key for key, value in scores.items() if value is None]
    if unresolved:
        logger.warning("以下指标全部为 NaN（裁判输出无法解析）: %s", unresolved)
    return scores, per_sample


def _select_metrics(requested: list[str]) -> list[str]:
    """从请求的指标里挑出当前可用的，并说明哪些被跳过。"""
    skipped = [name for name in requested if name in REQUIRES_EMBEDDING]
    if skipped:
        logger.warning("以下指标需要 embedding 模型，当前跳过: %s", skipped)
    selected = [name for name in requested if name in LEGACY_METRIC_NAMES]
    if not selected:
        raise ConfigError(f"没有可用指标（可选: {sorted(LEGACY_METRIC_NAMES)}；请求: {requested}）")
    return selected


def score(rows: list[dict[str, Any]], settings: Settings) -> dict[str, Any]:
    """对采集结果做 RAGAS 打分。需要 ``eval`` 可选依赖。"""
    try:
        from ragas import EvaluationDataset, evaluate
    except ImportError as exc:
        raise ConfigError("未安装 ragas。执行 `pip install -e '.[eval]'` 后再跑评测") from exc

    from app.judge import ProjectLLMJudge

    requested = settings.metric_list()
    selected = _select_metrics(requested)

    ragas_rows = _to_ragas_rows(rows)
    if not ragas_rows:
        return {
            "metrics": {},
            "judge_model": None,
            "scored_count": 0,
            "note": "没有可用于 L2 的样本",
        }

    # 必须把 judge_model 传下去：留空才回落到 LLM_PROVIDER 的默认模型。
    # 不传的话 JUDGE_MODEL 会被静默忽略——想做"强模型作答 + 便宜模型当裁判"
    # （或反过来）时，配置看起来生效了其实没有。
    judge = ProjectLLMJudge(settings, model=settings.judge_model or None)
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

    run_config = RunConfig(timeout=int(settings.judge_timeout), max_workers=settings.judge_workers)
    result = evaluate(
        dataset=EvaluationDataset.from_list(ragas_rows),
        metrics=[metric for _, metric in metrics],
        raise_exceptions=False,
        run_config=run_config,
    )
    # evaluate() 返回类型标注是 EvaluationResult | Executor（Executor 是内部执行器），这里只用
    # 结果表，所以显式收窄——避免内部执行器类型泄漏到业务代码。
    frame = result.to_pandas() if hasattr(result, "to_pandas") else result
    scores, per_sample = _aggregate(frame, metrics)

    return {
        "metrics": scores,
        "per_sample": per_sample,
        "judge_model": judge.target_name,
        # 端点实际服务的模型名（与请求名不同时才有值，例如中转把 chat 映射成 flash）
        "judge_served_model": judge.served_name or None,
        "judge_usage": judge.usage,
        "requested_metrics": requested,
        "scored_count": len(ragas_rows),
    }


__all__ = ["LEGACY_METRIC_NAMES", "REQUIRES_EMBEDDING", "score"]
