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

import re
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
    _apply_equivalence_rules(loaded)
    return loaded


# 裁判等价规则。为什么必须有：语料与答案都是正常中文，但表示形式常常不同——
# 语料写「五百元」、答案写「500元」；语料写「十点」、答案写「10:00」。
# RAGAS 默认的 NLI 判据是「可否**直接推断**」，裁判按字面匹配就判 0，
# 把"模型说得对"报成"模型编造了"（假阴性，方向完全相反）。
#
# 边界刻意收窄：只放宽**表示形式**的等价，不放宽事实——
# 加了上下文里没有的数量/主体/条件仍然判 0，否则指标会松成"什么都对"。
EQUIVALENCE_RULES = """
Equivalence rules you MUST apply before giving a verdict:
- Chinese numerals and Arabic numerals express the SAME value: 五百 = 500, 三千 = 3000,
  十二个月 = 12 months. If the context states the amount in either notation, verdict is 1.
- Same for other formats: 十点 = 10:00, 三天 = 3 days, 百分之五十 = 50%.
- Rephrasing that preserves the fact (synonyms, different word order, 须/需要/必须
  politeness variants) is still directly inferable: verdict 1.
- These rules cover REPRESENTATION ONLY. A statement that adds facts, amounts,
  parties or conditions absent from the context remains verdict 0.
"""

# statement 拆分规则。根因：faithfulness 的假阴性大头不在 NLI 判据，
# 而在**拆分器**把答案拆坏。用探针逐条看 verdict 理由后确认的三种拆坏方式：
#   1) 把**问题里的前提**带进 statement（问"超过三千元需要谁审批"，
#      拆出"费用超过三千元"——上下文当然不会断言某笔具体费用，于是判 0）；
#   2) 把一个完整主张拆成**互相重叠的碎片**（"A 与 B 共同审批"拆成
#      "须由 A 审批" + "须由 B 审批"——两条单独看都不成立，各判 0）；
#   3) 引用标记（在 _to_ragas_rows 里剥离）。
STATEMENT_RULES = """
Additional rules for decomposing the answer:
- Do NOT carry premises or qualifiers from the question into statements
  (question: "for fees over 3000, who approves?" -> do NOT produce
  "the fee is over 3000" as a statement; that premise is not asserted by the answer).
- Each statement must be a COMPLETE, self-contained claim. Never split one claim
  into overlapping partial fragments: "approved jointly by A and B" must not become
  "approved by A" plus "approved by B" -- each fragment alone asserts something false.
- Keep the original meaning; do not add or remove conditions.
"""


def _apply_equivalence_rules(metrics: list[tuple[str, Any]]) -> None:
    """把等价规则与拆分规则写进 faithfulness 的两段提示词。

    只动 faithfulness：它的"拆 statement 再逐条对照"形态正是假阴性的来源；
    context_precision/recall 没有这个失败模式（实测 1.0），
    不为一致性顺手去动没出问题的指标。
    """
    for key, metric in metrics:
        if key != "faithfulness":
            continue
        nli = getattr(metric, "nli_statements_prompt", None)
        if nli is not None and hasattr(nli, "instruction"):
            if "Equivalence rules" not in nli.instruction:
                nli.instruction = f"{nli.instruction}\n{EQUIVALENCE_RULES.strip()}"
        else:
            logger.warning("faithfulness 没有 nli_statements_prompt，等价规则未生效")

        gen = getattr(metric, "statement_generator_prompt", None)
        if gen is not None and hasattr(gen, "instruction"):
            if "Additional rules" not in gen.instruction:
                gen.instruction = f"{gen.instruction}\n{STATEMENT_RULES.strip()}"
        else:
            logger.warning("faithfulness 没有 statement_generator_prompt，拆分规则未生效")


_CITE_MARKER = re.compile(r"\s*\[\d+\]")


def _to_ragas_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把采集行转成 RAGAS 需要的字段（跳过负样本与失败样本）。"""
    converted: list[dict[str, Any]] = []
    for row in rows:
        if row.get("should_refuse") or row.get("error") or not row.get("answer"):
            continue
        converted.append(
            {
                # sample_id 必须留在我们自己的行里：RAGAS 0.4 的 from_list 只保留
                # 它认识的 4 个字段，自定义键会被丢弃（实测 features() 里没有它），
                # 所以逐样本明细只能按数据集顺序配回去。
                "sample_id": row["sample_id"],
                "user_input": row["question"],
                # 剥离 [1][2] 这类引用标记：那是我们自己的引用语法，不是答案内容。
                # 不剥的话拆分器会把"要求来源于引用 [1]"当成一条主张——
                # 上下文里当然没有"[1]"这个东西，于是一条正确的答案被判 0
                # （实测 perm-eng-allowed 因此只有 0.5）。
                "response": _CITE_MARKER.sub("", row.get("answer", "")).strip(),
                "retrieved_contexts": list(row.get("contexts") or []) or [""],
                "reference": row.get("reference") or row.get("answer", ""),
            }
        )
    return converted


def _per_sample_by_id(
    frame: Any, per_sample: dict[str, list[float | None]], sample_ids: list[str]
) -> list[dict[str, Any]]:
    """把逐样本分数与 ``sample_id`` 对齐（RAGAS 0.4 不保留自定义列，只能按顺序映射）。

    为什么不能靠列：``EvaluationDataset.from_list`` 只保留它认识的 4 个字段
    （user_input / response / retrieved_contexts / reference），``sample_id`` 会被丢掉——
    实测 ``features()`` 里确实没有它。所以 id 只能由我们自己按**数据集顺序**配回去。

    这个映射依赖"RAGAS 按数据集顺序返回逐样本分数"。为把风险挡住，这里做**行数校验**：
    结果行数与输入条数不一致就返回空列表并告警，**绝不返回可能错位的 id**——
    分数挂到错的样本上比没有分数更危险：它会让人去修一个其实没问题的样本。
    """
    try:
        row_count = len(frame)
    except Exception:  # noqa: BLE001 - 拿不到就当对不齐，绝不猜
        row_count = -1
    scores = per_sample.get("faithfulness") or []
    if row_count != len(sample_ids) or len(scores) != len(sample_ids):
        logger.warning(
            "结果行数(%s) / 分数条数(%s) / 输入条数(%s) 不一致，L2 逐样本明细不标注样本",
            row_count,
            len(scores),
            len(sample_ids),
        )
        return []
    keys = sorted(per_sample)
    return [
        {"sample_id": sample_id, **{key: per_sample[key][index] for key in keys}}
        for index, sample_id in enumerate(sample_ids)
    ]


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
    sample_ids = [str(row["sample_id"]) for row in ragas_rows]

    return {
        "metrics": scores,
        "per_sample": per_sample,
        # 带样本 id 的逐样本明细：L2 分数低时能直接定位到是哪条问题
        "per_sample_by_id": _per_sample_by_id(frame, per_sample, sample_ids),
        # 归因字段：裁判判据里注入了表示形式等价规则（中文数字/格式/同义改写）。
        # 没有它，"L2 分数上来了"无法区分是修复了假阴性还是换了模型。
        "judge_equivalence_rules": True,
        "judge_model": judge.target_name,
        # 端点实际服务的模型名（与请求名不同时才有值，例如中转把 chat 映射成 flash）
        "judge_served_model": judge.served_name or None,
        "judge_usage": judge.usage,
        "requested_metrics": requested,
        "scored_count": len(ragas_rows),
    }


__all__ = ["LEGACY_METRIC_NAMES", "REQUIRES_EMBEDDING", "score"]
