"""Async SQLAlchemy session lifecycle for HTTP and worker services."""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from youtube_copy.config import get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_database_engine() -> AsyncEngine:
    """Create one async engine per process without connecting eagerly."""
    global _engine, _session_factory
    if _engine is None:
        _engine = create_async_engine(get_settings().load_database_url(), pool_pre_ping=True)
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """Yield a request-scoped session; services control transaction commits."""
    get_database_engine()
    if _session_factory is None:  # pragma: no cover - guarded by get_database_engine
        raise RuntimeError("database session factory was not initialized")
    async with _session_factory() as session:
        yield session


async def dispose_database_engine() -> None:
    """Close the process pool during application shutdown."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
