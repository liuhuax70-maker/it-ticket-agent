"""Checkpointer 工厂。

MVP 默认使用 SQLite（`langgraph-checkpoint-sqlite`），生产可平滑切换 Postgres / Redis；
配置 `CHECKPOINTER_BACKEND=memory` 时使用内存实现（仅测试/演示，进程重启即丢）。

会话键：`thread_id = session_id`，用于图状态恢复、人工审核挂起与多轮续跑。
"""

import sqlite3
from functools import lru_cache
from pathlib import Path

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


@lru_cache
def get_checkpointer():
    """按配置返回 Checkpointer 单例。"""
    settings = get_settings()

    if settings.checkpointer_backend == "memory":
        from langgraph.checkpoint.memory import InMemorySaver

        logger.warning("使用内存 Checkpointer：进程重启后会话状态会丢失（仅适合演示/测试）")
        return InMemorySaver()

    from langgraph.checkpoint.sqlite import SqliteSaver

    path = Path(settings.checkpointer_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # check_same_thread=False：FastAPI 的线程池会跨线程访问同一连接
    conn = sqlite3.connect(str(path), check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()  # 首次运行建表，幂等
    logger.info("使用 SQLite Checkpointer: %s", path)
    return saver


def session_thread_id(session_id: str) -> str:
    """会话 → 图线程 ID 映射。"""
    return session_id


def thread_config(session_id: str) -> dict:
    """`graph.invoke` / `graph.get_state` 需要的 config。"""
    return {"configurable": {"thread_id": session_thread_id(session_id)}}
