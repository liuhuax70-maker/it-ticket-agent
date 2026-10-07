"""eval 入口：跑评测并产出报告。

两个入口的定位：
    POST /eval/preflight   只做前置检查（语料是否入库、鉴权是否可用），不调用模型，秒级返回
    POST /eval/run         完整评测：采集 -> L1 -> （可选）L2 RAGAS
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from pydantic import BaseModel

from app.collector import preflight
from app.config import Settings
from app.datasets import load_samples
from app.reports import summarize, write_report
from app.runner import rescore, run
from packages.common.constants import SERVICE_EVAL, VERSION
from packages.common.errors import NotFoundError, ValidationError, install_exception_handlers
from packages.common.logging import get_logger, setup_logging
from packages.common.settings import load_settings
from packages.contracts import HealthResponse
from packages.observability import init_otel

settings: Settings = load_settings(Settings)


class EvalRunRequest(BaseModel):
    dataset_path: str | None = None
    limit: int | None = None
    write_report: bool = True
    # None = 用配置里的 ragas_enabled；False = 只跑 L1（快，适合迭代）
    run_ragas: bool | None = None


class PreflightRequest(BaseModel):
    dataset_path: str | None = None
    limit: int | None = None


class EvalScoreRequest(BaseModel):
    """离线重打分：复用已落盘的采集结果，只重跑 L2。

    存在的意义：裁判模型/指标/超时策略的调整不需要重新采集一遍
    （采集要打满真实链路，十几分钟起步），这也正是"采集与打分两段式"的价值。
    """

    report_path: str | None = None
    write_report: bool = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(settings.service_name, settings.log_level)
    logger = get_logger(settings.service_name)
    init_otel(settings.service_name, settings.otel_endpoint, settings.otel_enabled)
    app.state.settings = settings
    logger.info(
        "eval 启动 port=%s gateway=%s dataset=%s judge=%s",
        settings.port,
        settings.api_gateway_url,
        settings.dataset_path,
        settings.judge_model or settings.llm_provider,
    )
    yield


app = FastAPI(title="eval", version=VERSION, lifespan=lifespan)
install_exception_handlers(app)


@app.get("/eval/datasets")
async def datasets() -> dict[str, Any]:
    samples = load_samples(settings.dataset_path)
    return {
        "path": settings.dataset_path,
        "count": len(samples),
        "positive": sum(1 for s in samples if not s.should_refuse),
        "negative": sum(1 for s in samples if s.should_refuse),
        "identities": sorted({s.identity.describe() for s in samples}),
        "tags": sorted({tag for sample in samples for tag in sample.tags}),
        "metrics_l2": settings.metric_list(),
    }


@app.post("/eval/preflight")
async def eval_preflight(req: PreflightRequest) -> dict[str, Any]:
    samples = load_samples(req.dataset_path or settings.dataset_path, limit=req.limit)
    return await preflight(samples, settings)


@app.post("/eval/run")
async def eval_run(req: EvalRunRequest, request: Request) -> dict[str, Any]:  # noqa: ARG001
    dataset_path = req.dataset_path or settings.dataset_path
    limit = min(req.limit or settings.max_samples, settings.max_samples)
    samples = load_samples(dataset_path, limit=limit)

    payload = await run(samples, settings, with_ragas=req.run_ragas)
    summary = summarize(payload)

    report_path: str | None = None
    if req.write_report:
        report_path = write_report(
            settings.reports_dir, payload, name="baseline", dataset_path=dataset_path
        ).as_posix()

    return {
        "summary": summary,
        "report_path": report_path,
        "judge_model": (payload.get("l2") or {}).get("judge_model"),
        "preflight": payload["preflight"],
    }


@app.post("/eval/score")
async def eval_score(req: EvalScoreRequest) -> dict[str, Any]:
    """离线重打分：读已落盘的采集结果，只重跑 L1 + L2，不重新采集。

    注意 L1 是从保存的行**重算**的，而不是复用报告里的旧数字——
    行才是权威，报告里缓存的聚合值可能来自旧版指标实现。
    """
    path = (
        Path(req.report_path)
        if req.report_path
        else Path(settings.reports_dir) / "baseline_latest.json"
    )
    if not path.exists():
        raise NotFoundError(f"找不到已保存的采集结果: {path}（先跑一次 /eval/run）")

    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("samples") or []
    if not rows:
        raise ValidationError(f"{path} 里没有样本明细，无法重打分")

    result = await rescore(rows, settings)
    summary = summarize(result)

    report_path: str | None = None
    if req.write_report:
        report_path = write_report(
            settings.reports_dir,
            result,
            name="baseline",
            dataset_path=payload.get("dataset") or settings.dataset_path,
        ).as_posix()

    return {
        "summary": summary,
        "report_path": report_path,
        "source": str(path),
        "judge_model": (result.get("l2") or {}).get("judge_model"),
    }


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    details = {
        "dataset": settings.dataset_path,
        "reports_dir": settings.reports_dir,
        "gateway": settings.api_gateway_url,
        "top_k": str(settings.top_k),
        "judge_provider": settings.llm_provider,
        "metrics": settings.metrics,
    }
    status = "ok"
    # 裁判复用项目 LLMClient（app/judge.py），因此这里不再探测 langchain-ollama：
    # 探一个用不到的依赖会在健康检查里长期挂着一条假故障。
    details["judge_adapter"] = "ProjectLLMJudge（复用项目 LLMClient）"
    try:
        import ragas  # noqa: F401

        details["ragas"] = getattr(ragas, "__version__", "installed")
    except ImportError:
        details["ragas"] = "not installed（pip install -e '.[eval]'）"
        details["l1_only"] = "true（L1 确定性指标仍可用）"
        status = "degraded"

    return HealthResponse(
        status=status,  # type: ignore[arg-type]
        service=SERVICE_EVAL,
        version=VERSION,
        details=details,
    )


def run_server() -> None:  # pragma: no cover
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
