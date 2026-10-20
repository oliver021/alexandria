from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

import httpx
import pytest
from anyio.from_thread import BlockingPortal, start_blocking_portal
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.auth.local import LocalAuthProvider
from app.db.session import configure_sqlite_engine
from app.models import Base
from app.models.schemas import TokenCreate
from app.models.user import UserRole
from app.repositories import user_repo
from app.services.principal import Principal
from app.services.token_service import TokenService
from app.storage import LocalBlobStore
from tests.helpers.factories import make_principal
from tests.test_cli.support import LOCAL_URL, Result, run

T = TypeVar("T")

JWT_SECRET = "live-test-secret-live-test-secret"


class SyncASGITransport(httpx.BaseTransport):
    def __init__(self, app: Any, portal: BlockingPortal) -> None:
        self._transport = httpx.ASGITransport(app=app)
        self._portal = portal

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        forwarded = httpx.Request(
            request.method,
            request.url,
            headers=request.headers,
            content=request.read(),
            extensions=request.extensions,
        )
        return self._portal.call(self._exchange, forwarded)

    async def _exchange(self, request: httpx.Request) -> httpx.Response:
        response = await self._transport.handle_async_request(request)
        content = await response.aread()
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            content=content,
            request=request,
        )


@dataclass
class Live:
    portal: BlockingPortal
    factory: async_sessionmaker[AsyncSession]
    transport: SyncASGITransport
    principals: dict[str, Principal] = field(default_factory=dict)

    def call(self, function: Callable[..., Awaitable[T]], *args: Any) -> T:
        return self.portal.call(function, *args)

    def principal(self, username: str, *, role: UserRole = UserRole.PUBLISHER) -> Principal:
        if username not in self.principals:

            async def create() -> Principal:
                async with self.factory() as db:
                    return await make_principal(db, username, role=role)

            self.principals[username] = self.call(create)
        return self.principals[username]

    def token(
        self, username: str = "alice", *, scope: str = "write", role: UserRole = UserRole.PUBLISHER
    ) -> str:
        principal = self.principal(username, role=role)

        async def create() -> str:
            async with self.factory() as db:
                created = await TokenService(db).create(
                    principal, TokenCreate(name=f"live-{scope}", scope=scope)
                )
                return created.token

        return self.call(create)

    def jwt(self, username: str = "alice") -> str:
        principal = self.principal(username)

        async def create() -> str:
            async with self.factory() as db:
                user = await user_repo.get_user(db, principal.user_id)
                assert user is not None
                return await LocalAuthProvider(secret=JWT_SECRET).create_token(user)

        return self.call(create)

    def run(
        self, *argv: object, token: str | None = None, env: Mapping[str, str] | None = None
    ) -> Result:
        variables = {"ALX_URL": LOCAL_URL, **(env or {})}
        if token is not None:
            variables["ALX_TOKEN"] = token
        return run(*argv, transport=self.transport, env=variables)


def live_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Live]:
    from app.config.settings import settings
    from app.db.session import get_db
    from app.main import app
    from app.storage import get_blob_store

    monkeypatch.setattr(settings.auth, "jwt_secret", JWT_SECRET)
    store = LocalBlobStore(tmp_path / "blobs")
    with start_blocking_portal() as portal:

        async def prepare():
            engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
            configure_sqlite_engine(engine)
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            return engine, async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        engine, factory = portal.call(prepare)

        async def override_get_db():
            async with factory() as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_blob_store] = lambda: store
        try:
            yield Live(portal, factory, SyncASGITransport(app, portal))
        finally:
            app.dependency_overrides.clear()
            portal.call(engine.dispose)


@dataclass
class Served:
    live: Live
    url: str

    def run(
        self, *argv: object, token: str | None = None, env: Mapping[str, str] | None = None
    ) -> Result:
        variables = {"ALX_URL": self.url, **(env or {})}
        if token is not None:
            variables["ALX_TOKEN"] = token
        return run(*argv, env=variables)


STARTUP_SECONDS = 15
SHUTDOWN_SECONDS = 15


def served_over_http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Served]:
    import uvicorn

    from app.main import app

    for live in live_server(tmp_path, monkeypatch):
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off")
        server = uvicorn.Server(config)
        task = live.portal.start_task_soon(server.serve)
        deadline = time.monotonic() + STARTUP_SECONDS
        while not server.started:
            if time.monotonic() > deadline or task.done():
                server.should_exit = True
                raise RuntimeError("the test server did not start")
            time.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        try:
            yield Served(live, f"http://127.0.0.1:{port}")
        finally:
            server.should_exit = True
            task.result(timeout=SHUTDOWN_SECONDS)
