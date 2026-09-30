"""FastAPI 应用入口。

职责：
1. 创建 FastAPI 实例并挂载统一路由前缀（/api/v1）。
2. 注入 trace_id 中间件与全局异常处理器。
3. 生命周期内初始化日志、可观测等基础设施。
"""

from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request

from app import __version__
from app.api.router import api_router
from app.core.config import get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import get_logger, setup_logging

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(settings.log_level)
    logger.info("app 启动 env=%s version=%s", settings.app_env, __version__)
    # TODO(后续)：在此接入 LangSmith、Milvus 连接池、Checkpointer
    yield
    logger.info("app 停止")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="IT/客服工单智能处理助手",
        description="企业内部 IT/客服工单智能处理助手 API",
        version=__version__,
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def add_trace_id(request: Request, call_next):
        """为每个请求分配 trace_id，并回写到响应头。"""
        trace_id = request.headers.get("X-Trace-Id") or uuid4().hex[:16]
        request.state.trace_id = trace_id
        response = await call_next(request)
        response.headers["X-Trace-Id"] = trace_id
        return response

    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.api_prefix)
    return app


app = create_app()


@app.get("/", tags=["meta"], summary="根路径")
async def root() -> dict:
    """服务元信息（便于快速确认服务已启动）。"""
    settings = get_settings()
    return {
        "app": settings.app_name,
        "version": __version__,
        "env": settings.app_env,
        "docs": "/docs",
        "api_prefix": settings.api_prefix,
    }
