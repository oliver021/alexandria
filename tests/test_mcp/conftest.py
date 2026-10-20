from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config.settings import settings
from app.mcp.asgi import McpGateway
from app.mcp.context import configure_runtime, reset_runtime
from app.storage import LocalBlobStore
from tests.helpers.factories import Actors
from tests.test_mcp.support import (
    SECRET,
    Credentials,
    Registry,
    issue_credentials,
    seed_registry,
)


@pytest.fixture(autouse=True)
def mcp_runtime(
    session_factory: async_sessionmaker[AsyncSession], blob_store: LocalBlobStore
) -> Iterator[None]:
    configure_runtime(session_factory=session_factory, blob_store=blob_store)
    yield
    reset_runtime()


@pytest.fixture(autouse=True)
def jwt_secret(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(settings.auth, "jwt_secret", SECRET)
    return SECRET


@pytest_asyncio.fixture
async def registry(
    session_factory: async_sessionmaker[AsyncSession], blob_store: LocalBlobStore, actors: Actors
) -> Registry:
    return await seed_registry(session_factory, blob_store, actors)


@pytest_asyncio.fixture
async def credentials(
    session_factory: async_sessionmaker[AsyncSession], actors: Actors
) -> Credentials:
    return await issue_credentials(session_factory, actors)


@pytest_asyncio.fixture
async def gateway() -> AsyncIterator[McpGateway]:
    app = McpGateway()
    ready = asyncio.Event()
    stop = asyncio.Event()
    failures: list[BaseException] = []

    async def serve() -> None:
        try:
            async with app.lifespan():
                ready.set()
                await stop.wait()
        except BaseException as exc:
            failures.append(exc)
            ready.set()
            raise

    task = asyncio.create_task(serve())
    await ready.wait()
    if failures:
        await task
    try:
        yield app
    finally:
        stop.set()
        await task
