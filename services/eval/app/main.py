"""eval 入口：跑评测并产出报告。

两个入口的定位：
    POST /eval/preflight   只做前置检查（语料是否入库、鉴权是否可用），不调用模型，秒级返回
    POST /eval/run         完整评测：采集 -> L1 -> （可选）L2 RAGAS
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from pydantic import BaseModel

from app.collector import preflight
from app.config import Settings
from app.datasets import load_samples
from app.reports import summarize, write_report
from app.runner import run
from packages.common.constants import SERVICE_EVAL, VERSION
from packages.common.errors import install_exception_handlers
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
    try:
        import ragas  # noqa: F401

        details["ragas"] = getattr(ragas, "__version__", "installed")
    except ImportError:
        details["ragas"] = "not installed（pip install -e '.[eval]'）"
        status = "degraded"

    try:
        from langchain_ollama import ChatOllama  # noqa: F401

        details["judge_local"] = "langchain-ollama"
    except ImportError:
        details["judge_local"] = "缺少 langchain-ollama（本地裁判不可用）"

    return HealthResponse(
        status=status,  # type: ignore[arg-type]
        service=SERVICE_EVAL,
        version=VERSION,
        details=details,
    )


def run_server() -> None:  # pragma: no cover
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
