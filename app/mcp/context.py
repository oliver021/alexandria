from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.db import session as database
from app.services.principal import Principal
from app.storage import BlobStore

SessionFactory = Callable[[], AsyncSession]


@dataclass(frozen=True)
class RuntimeConfig:
    session_factory: SessionFactory | None = None
    blob_store: BlobStore | None = None


_principal: ContextVar[Principal | None] = ContextVar("alexandria_mcp_principal", default=None)
_runtime = RuntimeConfig()


def current_principal() -> Principal | None:
    return _principal.get()


@contextmanager
def use_principal(principal: Principal | None) -> Iterator[None]:
    token = _principal.set(principal)
    try:
        yield
    finally:
        _principal.reset(token)


def configure_runtime(
    *, session_factory: SessionFactory | None = None, blob_store: BlobStore | None = None
) -> None:
    global _runtime
    _runtime = RuntimeConfig(
        session_factory=session_factory
        if session_factory is not None
        else _runtime.session_factory,
        blob_store=blob_store if blob_store is not None else _runtime.blob_store,
    )


def reset_runtime() -> None:
    global _runtime
    _runtime = RuntimeConfig()


def get_session_factory() -> SessionFactory:
    if _runtime.session_factory is not None:
        return _runtime.session_factory
    return database.async_session_factory


def get_blob_store() -> BlobStore:
    if _runtime.blob_store is not None:
        return _runtime.blob_store
    return storage.get_blob_store()


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    async with get_session_factory()() as session:
        yield session
