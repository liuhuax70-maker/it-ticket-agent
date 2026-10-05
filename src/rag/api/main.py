"""FastAPI 应用入口：只做装配，不写业务逻辑。

启动（在仓库根目录执行，保证 config 包与 .env 可被找到）::

    python -m uvicorn rag.api.main:app --app-dir src --reload
"""

from fastapi import FastAPI

from rag.api.routes import health


def create_app() -> FastAPI:
    """装配应用：注册路由、挂载横切能力。"""
    app = FastAPI(
        title="permission-aware-rag",
        description="企业级权限感知 RAG 知识库平台",
        version="0.1.0",
    )
    app.include_router(health.router)
    # S6 接入：app.include_router(chat.router)
    return app


app = create_app()
