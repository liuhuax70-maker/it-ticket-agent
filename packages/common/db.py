"""Postgres 异步引擎与会话工厂（SQLAlchemy 2.0 + asyncpg）。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from packages.common.models import Base

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    """进程级单例引擎。"""
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            database_url,
            echo=echo,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
        )
    return _engine


def get_session_factory(database_url: str, *, echo: bool = False) -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(database_url, echo=echo), expire_on_commit=False
        )
    return _session_factory


@asynccontextmanager
async def session_scope(database_url: str) -> AsyncIterator[AsyncSession]:
    """事务边界：正常提交，异常回滚。"""
    factory = get_session_factory(database_url)
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def create_all(database_url: str) -> None:
    """开发/测试兜底建表；正式环境请用 Alembic 迁移。"""
    engine = get_engine(database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
