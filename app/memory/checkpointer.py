"""Checkpointer 工厂。

**为什么用异步实现**：接入层用 FastAPI，图通过 `astream` / `ainvoke` 驱动，
而同步的 `SqliteSaver` 对任何 async 方法都会抛
`NotImplementedError: The SqliteSaver does not support async methods`。
因此 SQLite 后端改用 `AsyncSqliteSaver`。

| backend | 实现 | 适用 |
| --- | --- | --- |
| `sqlite`（默认） | `AsyncSqliteSaver` | 生产/开发，持久化到文件 |
| `memory` | `InMemorySaver` | 测试/演示，进程重启即丢 |

会话键：`thread_id = session_id`。
"""

from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_saver: Any = None
_conn: Any = None


async def get_checkpointer():
    """返回 Checkpointer 单例（首次调用时异步初始化）。"""
    global _saver, _conn

    if _saver is not None:
        return _saver

    settings = get_settings()

    if settings.checkpointer_backend == "memory":
        from langgraph.checkpoint.memory import InMemorySaver

        logger.warning("使用内存 Checkpointer：进程重启后会话状态会丢失（仅适合演示/测试）")
        _saver = InMemorySaver()
        return _saver

    import aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    path = Path(settings.checkpointer_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    _conn = await aiosqlite.connect(str(path))
    _saver = AsyncSqliteSaver(_conn)
    await _saver.setup()  # 首次运行建表，幂等
    logger.info("使用 AsyncSqliteSaver: %s", path)
    return _saver


async def close_checkpointer() -> None:
    """关闭底层连接（应用退出时调用）。"""
    global _saver, _conn

    if _conn is not None:
        await _conn.close()
        logger.info("Checkpointer 连接已关闭")
    _saver = None
    _conn = None


def session_thread_id(session_id: str) -> str:
    """会话 → 图线程 ID 映射。"""
    return session_id


def thread_config(session_id: str) -> dict:
    """`graph.ainvoke` / `graph.aget_state` 需要的 config。"""
    return {"configurable": {"thread_id": session_thread_id(session_id)}}


def streaming_thread_config(session_id: str) -> dict:
    """SSE 场景的 config：在 thread_id 基础上要求草稿节点逐 token 流式输出。"""
    return {
        "configurable": {
            "thread_id": session_thread_id(session_id),
            "stream_tokens": True,
        }
    }
