"""Checkpointer 工厂。

MVP 使用 SQLite（langgraph-checkpoint-sqlite），
生产可平滑切换到 Postgres / Redis。

会话键：thread_id = session_id，用于图状态恢复与多轮续跑。
"""

from app.core.config import get_settings


def get_checkpointer():
    """按配置返回 Checkpointer 实例（带缓存的单例）。"""
    settings = get_settings()
    # TODO(后续)：根据 settings.checkpointer_backend 返回 SqliteSaver 等实现
    raise NotImplementedError(
        f"骨架占位：Checkpointer({settings.checkpointer_backend}) 将在后续编码阶段实现"
    )


def session_thread_id(session_id: str) -> str:
    """会话 → 图线程 ID 映射。"""
    return session_id
