from __future__ import annotations

import asyncio
import gc
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from passlib.context import CryptContext
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.auth import local as local_module
from app.auth import password as password_module
from app.db.session import configure_sqlite_engine
from app.models import Base
from app.storage import LocalBlobStore
from tests.helpers.factories import Actors, make_actors

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
MCP_BASE_URL = "http://test"
MCP_ENDPOINT = f"{MCP_BASE_URL}/mcp"
MCP_TIMEOUT_SECONDS = 30
MCP_CLIENT_LIFETIME_SECONDS = 300

McpConnector = Callable[..., AbstractAsyncContextManager[Any]]


def pytest_collection_finish(session: pytest.Session) -> None:
    gc.collect()
    gc.freeze()


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    test_engine = create_async_engine(TEST_DATABASE_URL, poolclass=StaticPool)
    configure_sqlite_engine(test_engine)
    async with test_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield test_engine
    await test_engine.dispose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def db_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session


@pytest.fixture
def fast_password_hashing(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(
        password_module, "pwd_context", CryptContext(schemes=["bcrypt"], bcrypt__rounds=4)
    )
    local_module._decoy_password_hash.cache_clear()
    yield
    local_module._decoy_password_hash.cache_clear()


@pytest.fixture
def blob_store(tmp_path: Path) -> LocalBlobStore:
    return LocalBlobStore(tmp_path / "blobs")


@pytest_asyncio.fixture
async def actors(db_session: AsyncSession) -> Actors:
    return await make_actors(db_session)


@pytest_asyncio.fixture
async def client(session_factory: async_sessionmaker[AsyncSession], blob_store: LocalBlobStore):
    from httpx import ASGITransport, AsyncClient

    from app.db.session import get_db
    from app.main import app
    from app.mcp import configure_runtime, reset_runtime
    from app.storage import get_blob_store

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_blob_store] = lambda: blob_store
    configure_runtime(session_factory=session_factory, blob_store=blob_store)
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as http_client:
            yield http_client
    finally:
        app.dependency_overrides.clear()
        reset_runtime()


@pytest_asyncio.fixture
async def mcp_server(client: Any) -> AsyncIterator[None]:
    from app.mcp import mcp_lifespan

    started = asyncio.Event()
    release = asyncio.Event()
    failures: list[BaseException] = []

    async def serve() -> None:
        try:
            async with mcp_lifespan():
                started.set()
                await release.wait()
        except BaseException as exc:
            failures.append(exc)
            started.set()
            raise

    task = asyncio.create_task(serve())
    await started.wait()
    if failures:
        await task
    try:
        yield
    finally:
        release.set()
        await task


@pytest.fixture
def mcp_connect(mcp_server: None) -> McpConnector:
    @asynccontextmanager
    async def connect(
        token: str | None = None,
        *,
        mode: str = "legacy",
        headers: Mapping[str, str] | None = None,
        timeout: float = MCP_TIMEOUT_SECONDS,
    ) -> AsyncIterator[Any]:
        import httpx2
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client

        from app.main import app

        request_headers = dict(headers or {})
        if token is not None:
            request_headers["Authorization"] = f"Bearer {token}"
        async with asyncio.timeout(timeout):
            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app),
                base_url=MCP_BASE_URL,
                headers=request_headers,
            ) as http:
                transport = streamable_http_client(MCP_ENDPOINT, http_client=http)
                async with Client(transport, mode=mode) as session:
                    yield session

    return connect


@pytest_asyncio.fixture
async def mcp_client(mcp_connect: McpConnector) -> AsyncIterator[Any]:
    connected = asyncio.Event()
    release = asyncio.Event()
    opened: list[Any] = []
    failures: list[BaseException] = []

    async def hold() -> None:
        try:
            async with mcp_connect(timeout=MCP_CLIENT_LIFETIME_SECONDS) as session:
                opened.append(session)
                connected.set()
                await release.wait()
        except BaseException as exc:
            failures.append(exc)
            connected.set()
            raise

    task = asyncio.create_task(hold())
    await connected.wait()
    if failures:
        await task
    try:
        yield opened[0]
    finally:
        release.set()
        await task
