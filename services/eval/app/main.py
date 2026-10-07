"""eval 入口：跑 RAGAS 评测并产出报告。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from pydantic import BaseModel

from packages.common.constants import SERVICE_EVAL, VERSION
from packages.common.errors import install_exception_handlers
from packages.common.logging import get_logger, setup_logging
from packages.common.settings import load_settings
from packages.contracts import HealthResponse
from packages.observability import init_otel

from app.config import Settings
from app.datasets import ensure_dataset, load_samples
from app.ragas_runner import run
from app.reports import summarize, write_report

settings: Settings = load_settings(Settings)


class EvalRunRequest(BaseModel):
    dataset_path: str | None = None
    limit: int | None = None
    write_report: bool = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(settings.service_name, settings.log_level)
    logger = get_logger(settings.service_name)
    init_otel(settings.service_name, settings.otel_endpoint, settings.otel_enabled)
    app.state.settings = settings
    ensure_dataset(settings.dataset_path)
    logger.info(
        "eval 启动 port=%s gateway=%s judge=%s",
        settings.port,
        settings.api_gateway_url,
        settings.judge_model or settings.llm_provider,
    )
    yield


app = FastAPI(title="eval", version=VERSION, lifespan=lifespan)
install_exception_handlers(app)


@app.get("/eval/datasets")
async def datasets(request: Request) -> dict[str, Any]:
    path = ensure_dataset(settings.dataset_path)
    samples = load_samples(path)
    return {
        "path": path.as_posix(),
        "count": len(samples),
        "tags": sorted({tag for sample in samples for tag in sample.tags}),
        "metrics": settings.metric_list(),
    }


@app.post("/eval/run")
async def eval_run(req: EvalRunRequest, request: Request) -> dict[str, Any]:  # noqa: ARG001
    dataset_path = req.dataset_path or settings.dataset_path
    limit = min(req.limit or settings.max_samples, settings.max_samples)
    samples = load_samples(dataset_path, limit=limit)

    payload = await run(samples, settings)
    summary = summarize(payload)

    report_path: str | None = None
    if req.write_report:
        report_path = write_report(
            settings.reports_dir, payload, name="ragas", dataset_path=dataset_path
        ).as_posix()

    return {"summary": summary, "report_path": report_path, "judge_model": payload.get("judge_model")}


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    from app.ragas_runner import score  # noqa: F401  仅用于探测依赖是否可导入

    details = {
        "dataset": settings.dataset_path,
        "reports_dir": settings.reports_dir,
        "gateway": settings.api_gateway_url,
        "judge_provider": settings.llm_provider,
        "metrics": settings.metrics,
    }
    try:
        import ragas  # noqa: F401

        details["ragas"] = getattr(ragas, "__version__", "installed")
        status = "ok"
    except ImportError:
        details["ragas"] = "not installed（pip install -e '.[eval]'）"
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
