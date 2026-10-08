"""Postgres 异步引擎与会话工厂（SQLAlchemy 2.0 + asyncpg）。

按 **URL 键控**缓存引擎/会话工厂。曾实现为"首个 URL 永久生效"的单例：
进程里第二个库（例如网关同时触主库与审计库）的读写会**静默落到第一个库**，
没有任何报错——那是最阴险的一类 bug。
"""

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

_engines: dict[str, AsyncEngine] = {}
_session_factories: dict[str, async_sessionmaker[AsyncSession]] = {}


def get_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    engine = _engines.get(database_url)
    if engine is None:
        engine = create_async_engine(
            database_url,
            echo=echo,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
        )
        _engines[database_url] = engine
    return engine


def get_session_factory(
    database_url: str, *, echo: bool = False
) -> async_sessionmaker[AsyncSession]:
    factory = _session_factories.get(database_url)
    if factory is None:
        factory = async_sessionmaker(
            bind=get_engine(database_url, echo=echo), expire_on_commit=False
        )
        _session_factories[database_url] = factory
    return factory


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


async def dispose_engine(database_url: str | None = None) -> None:
    """关闭引擎。传 URL 只关那一个；不传清空全部（测试用）。"""
    urls = [database_url] if database_url else list(_engines)
    for url in urls:
        engine = _engines.pop(url, None)
        if engine is not None:
            await engine.dispose()
        _session_factories.pop(url, None)
