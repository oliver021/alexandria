from collections.abc import Callable
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import QueuePool

from app.config.settings import DatabaseSettings, settings


def _set_sqlite_foreign_keys(dbapi_connection, connection_record) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def _unicode_lower(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return str(value).lower()


def _register_unicode_lower(dbapi_connection, connection_record) -> None:
    dbapi_connection.create_function("lower", 1, _unicode_lower, deterministic=True)


def _listen_on_sqlite(engine: AsyncEngine | Engine, listener: Callable[..., None]) -> None:
    target = engine.sync_engine if isinstance(engine, AsyncEngine) else engine
    if target.dialect.name == "sqlite":
        event.listen(target, "connect", listener)


def enable_sqlite_foreign_keys(engine: AsyncEngine | Engine) -> None:
    _listen_on_sqlite(engine, _set_sqlite_foreign_keys)


def enable_sqlite_unicode_lower(engine: AsyncEngine | Engine) -> None:
    _listen_on_sqlite(engine, _register_unicode_lower)


def configure_sqlite_engine(engine: AsyncEngine | Engine) -> None:
    enable_sqlite_foreign_keys(engine)
    enable_sqlite_unicode_lower(engine)


def pool_options(url: str, database: DatabaseSettings) -> dict[str, Any]:
    parsed = make_url(url)
    if not issubclass(parsed.get_dialect().get_pool_class(parsed), QueuePool):
        return {}
    return {
        "pool_size": database.pool_size,
        "max_overflow": database.max_overflow,
        "pool_timeout": database.pool_timeout,
    }


database_url = settings.database.url

engine = create_async_engine(
    database_url,
    echo=False,
    future=True,
    **pool_options(database_url, settings.database),
)
configure_sqlite_engine(engine)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db():
    async with async_session_factory() as session:
        yield session
