from __future__ import annotations

import asyncio
import random
import threading
import time
import zipfile
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO

import pytest
import pytest_asyncio
import uvicorn
from fastapi.responses import HTMLResponse
from httpx import ASGITransport, AsyncClient
from jose import jwt
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.api import public
from app.db.session import configure_sqlite_engine, get_db
from app.models import Base
from app.storage import BlobStore, LocalBlobStore, get_blob_store
from tests.helpers.factories import Actors, make_actors
from tests.helpers.packages import make_skill_zip
from tests.test_api.support import JWT_SECRET, Api, create_tokens

BOUNDARY = "connection-test-boundary"


class WatchingStore:
    def __init__(self, inner: LocalBlobStore, engine: AsyncEngine) -> None:
        self.inner = inner
        self.engine = engine
        self.checked_out_when_streaming: list[int] = []
        self.checked_out_when_opened: list[int] = []

    async def put(self, key: str, data: bytes) -> bool:
        return await self.inner.put(key, data)

    async def get(self, key: str) -> bytes:
        return await self.inner.get(key)

    async def open(self, key: str) -> BinaryIO:
        self.checked_out_when_opened.append(self.engine.pool.checkedout())
        return await self.inner.open(key)

    async def exists(self, key: str) -> bool:
        return await self.inner.exists(key)

    async def delete(self, key: str) -> bool:
        return await self.inner.delete(key)

    async def size(self, key: str) -> int:
        return await self.inner.size(key)

    def stream(self, key: str, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        async def watched() -> AsyncIterator[bytes]:
            self.checked_out_when_streaming.append(self.engine.pool.checkedout())
            async for chunk in self.inner.stream(key, chunk_size):
                yield chunk

        return watched()


@dataclass
class Pooled:
    engine: AsyncEngine
    api: Api
    store: WatchingStore
    actors: Actors

    def checked_out(self) -> int:
        return self.engine.pool.checkedout()


@asynccontextmanager
async def serving(tmp_path: Path, **pool: Any) -> AsyncIterator[Pooled]:
    from app.main import app

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pool.db'}", **pool)
    configure_sqlite_engine(engine)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    store = WatchingStore(LocalBlobStore(tmp_path / "blobs"), engine)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    async with factory() as session:
        actors = await make_actors(session)
        tokens = await create_tokens(session, actors)
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_blob_store] = lambda: store
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield Pooled(engine, Api(client, tokens), store, actors)
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()


@pytest_asyncio.fixture
async def pooled(tmp_path: Path, api_settings: None) -> AsyncIterator[Pooled]:
    async with serving(tmp_path) as handle:
        yield handle


@pytest_asyncio.fixture
async def tight(tmp_path: Path, api_settings: None) -> AsyncIterator[Pooled]:
    async with serving(tmp_path, pool_size=1, max_overflow=1, pool_timeout=3) as handle:
        yield handle


def multipart(data: bytes) -> list[bytes]:
    head = (
        f"--{BOUNDARY}\r\n"
        'Content-Disposition: form-data; name="file"; filename="p.zip"\r\n'
        "Content-Type: application/zip\r\n\r\n"
    ).encode()
    tail = f"\r\n--{BOUNDARY}--\r\n".encode()
    third = len(data) // 3
    return [head, data[:third], data[third : 2 * third], data[2 * third :], tail]


def jwt_for(user_id: str) -> str:
    claims = {"sub": user_id, "exp": datetime.now(UTC) + timedelta(minutes=5)}
    return jwt.encode(claims, JWT_SECRET, algorithm="HS256")


@pytest.mark.asyncio
@pytest.mark.parametrize("credential", ["api-token", "jwt"])
async def test_no_database_connection_is_held_while_an_upload_body_arrives(
    pooled: Pooled, credential: str
):
    headers = {"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"}
    if credential == "jwt":
        headers["Authorization"] = f"Bearer {jwt_for(pooled.actors.owner.user_id)}"
        actor = None
    else:
        await pooled.api.get("/auth/me", "owner")
        actor = "owner"
    observed: list[int] = []
    pieces = multipart(make_skill_zip("slow-upload", "1.0.0"))

    async def slow_body() -> AsyncIterator[bytes]:
        for piece in pieces:
            observed.append(pooled.checked_out())
            yield piece

    response = await pooled.api.post("/packages", actor, content=slow_body(), headers=headers)

    assert response.status_code == 201
    assert len(observed) == len(pieces)
    assert observed == [0] * len(pieces)
    assert pooled.checked_out() == 0


@pytest.mark.asyncio
async def test_no_database_connection_is_held_while_a_package_streams_out(pooled: Pooled):
    data = make_skill_zip("slow-download", "1.0.0")
    assert (await pooled.api.upload(data, "owner", publish=True)).status_code == 201

    response = await pooled.api.get("/skills/slow-download/versions/1.0.0/download")

    assert response.status_code == 200
    assert response.content == data
    assert pooled.store.checked_out_when_streaming == [0]
    assert pooled.checked_out() == 0


@pytest.mark.asyncio
async def test_authenticated_downloads_also_release_the_connection_before_streaming(
    pooled: Pooled,
):
    data = make_skill_zip("slow-private", "1.0.0")
    await pooled.api.upload(data, "owner")
    await pooled.api.get("/auth/me", "owner")

    response = await pooled.api.get("/skills/slow-private/versions/1.0.0/download", "owner")

    assert response.status_code == 200
    assert pooled.store.checked_out_when_streaming == [0]


@pytest.mark.asyncio
async def test_no_database_connection_is_held_while_a_raw_file_streams_out(pooled: Pooled):
    payload = random.Random(5).randbytes(300 * 1024)
    data = make_skill_zip(
        "slow-raw", "1.0.0", files={"payload.bin": payload}, compression=zipfile.ZIP_STORED
    )
    assert (await pooled.api.upload(data, "owner", publish=True)).status_code == 201

    response = await pooled.api.get("/skills/slow-raw/versions/1.0.0/raw/payload.bin")

    assert response.status_code == 200
    assert response.content == payload
    assert len(pooled.store.checked_out_when_opened) == 2
    assert pooled.store.checked_out_when_opened[1:] == [0]
    assert pooled.checked_out() == 0


@pytest.mark.asyncio
async def test_authenticated_raw_reads_also_release_the_connection_before_streaming(
    pooled: Pooled,
):
    payload = random.Random(6).randbytes(300 * 1024)
    data = make_skill_zip(
        "private-raw", "1.0.0", files={"payload.bin": payload}, compression=zipfile.ZIP_STORED
    )
    await pooled.api.upload(data, "owner")
    await pooled.api.get("/auth/me", "owner")

    response = await pooled.api.get("/skills/private-raw/versions/1.0.0/raw/payload.bin", "owner")

    assert response.status_code == 200
    assert response.content == payload
    assert pooled.store.checked_out_when_opened[-1] == 0
    assert pooled.checked_out() == 0


@pytest.mark.asyncio
async def test_many_slow_raw_readers_cannot_starve_the_pool(pooled: Pooled):
    payload = random.Random(7).randbytes(200 * 1024)
    data = make_skill_zip(
        "crowded-raw", "1.0.0", files={"payload.bin": payload}, compression=zipfile.ZIP_STORED
    )
    await pooled.api.upload(data, "owner", publish=True)
    pool_capacity = pooled.engine.pool.size() + 10

    responses = await asyncio.gather(
        *[
            pooled.api.get("/skills/crowded-raw/versions/1.0.0/raw/payload.bin")
            for _ in range(pool_capacity * 2)
        ]
    )

    assert all(
        response.status_code == 200 and response.content == payload for response in responses
    )
    assert pooled.checked_out() == 0


@pytest.mark.asyncio
async def test_no_request_leaves_a_connection_behind_whatever_its_outcome(pooled: Pooled):
    await pooled.api.upload(make_skill_zip("leak-check", "1.0.0"), "owner", publish=True)
    requests = [
        ("GET", "/skills"),
        ("GET", "/skills/leak-check"),
        ("GET", "/skills/missing"),
        ("GET", "/skills/leak-check/versions/9.9.9"),
        ("GET", "/skills/leak-check/versions/1.0.0/files/..%2Fx"),
        ("GET", "/skills/leak-check/versions/1.0.0/raw/SKILL.md"),
        ("GET", "/skills/leak-check/versions/1.0.0/download"),
        ("GET", "/skills?limit=0"),
        ("GET", "/auth/me"),
        ("POST", "/tokens"),
        ("DELETE", "/skills/leak-check"),
        ("POST", "/skills/leak-check/restore"),
    ]

    for method, path in requests:
        for actor in ("anonymous", "owner", "owner_read", "stranger"):
            await pooled.api.request(method, path, actor, json={"name": "x"})
            assert pooled.checked_out() == 0, (method, path, actor)

    broken = await pooled.api.upload(b"not a zip", "owner")
    assert broken.status_code == 422
    assert pooled.checked_out() == 0


@pytest.mark.asyncio
async def test_many_slow_downloads_cannot_starve_the_pool(pooled: Pooled):
    data = make_skill_zip("crowded", "1.0.0")
    await pooled.api.upload(data, "owner", publish=True)
    pool_capacity = pooled.engine.pool.size() + 10

    responses = await asyncio.gather(
        *[
            pooled.api.get("/skills/crowded/versions/1.0.0/download")
            for _ in range(pool_capacity * 2)
        ]
    )

    assert all(response.status_code == 200 for response in responses)
    assert pooled.checked_out() == 0


def test_the_watching_store_is_a_blob_store(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'x.db'}")

    assert isinstance(WatchingStore(LocalBlobStore(tmp_path), engine), BlobStore)


SITE_PATHS = [
    "/",
    "/registry",
    "/registry?tag=alpha&kind=skill&q=guide",
    "/registry/skills/site-pool",
    "/registry/skills/site-pool?version=1.0.0",
    "/registry/skills/site-pool/versions/1.0.0/files/docs/guide.md",
    "/registry/skills/missing-entry",
    "/registry?kind=nonsense",
    "/registry?page=0",
]


async def publish_site_entry(pooled: Pooled) -> None:
    data = make_skill_zip(
        "site-pool", "1.0.0", tags=["alpha"], files={"docs/guide.md": "# Guide\n\nHello\n"}
    )
    assert (await pooled.api.upload(data, "owner", publish=True)).status_code == 201


def request_scope(path: str) -> dict[str, Any]:
    target, _, query = path.partition("?")
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": target,
        "raw_path": target.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": [(b"host", b"test")],
        "client": ("127.0.0.1", 4321),
        "server": ("test", 80),
    }


async def stalled_client(path: str, gate: asyncio.Event, arrived: list[str]) -> int:
    from app.main import app

    delivered = False
    status = 0

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await gate.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]
            arrived.append(path)
            await gate.wait()

    await app(request_scope(path), receive, send)
    return status


async def wait_for(condition: Callable[[], bool], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "the condition never became true"
        await asyncio.sleep(0.005)


async def finish(tasks: list[asyncio.Task[Any]], grace: float = 5.0) -> list[Any]:
    _, stragglers = await asyncio.wait(tasks, timeout=grace)
    for task in stragglers:
        task.cancel()
    return await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_the_site_releases_its_connection_before_it_renders_anything(
    pooled: Pooled, monkeypatch: pytest.MonkeyPatch
):
    await publish_site_entry(pooled)
    rendered: list[int] = []
    formatted: list[int] = []
    original_render = public.render
    original_markdown = public.render_markdown

    def watching_render(*args: Any, **kwargs: Any):
        rendered.append(pooled.checked_out())
        return original_render(*args, **kwargs)

    async def watching_markdown(*args: Any, **kwargs: Any):
        formatted.append(pooled.checked_out())
        return await original_markdown(*args, **kwargs)

    monkeypatch.setattr(public, "render", watching_render)
    monkeypatch.setattr(public, "render_markdown", watching_markdown)
    public.MARKDOWN_CACHE.clear()

    for path in SITE_PATHS:
        response = await pooled.api.client.get(path)
        assert response.status_code in (200, 400, 404), (path, response.status_code)
        assert pooled.checked_out() == 0, path

    assert len(rendered) == len(SITE_PATHS)
    assert rendered == [0] * len(SITE_PATHS)
    assert formatted == [0, 0]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", SITE_PATHS)
async def test_twenty_clients_that_never_read_the_site_cannot_starve_the_pool(
    pooled: Pooled, path: str
):
    await publish_site_entry(pooled)
    clients = pooled.engine.pool.size() + 10 + 5
    assert clients >= 20
    gate = asyncio.Event()
    arrived: list[str] = []
    tasks = [asyncio.create_task(stalled_client(path, gate, arrived)) for _ in range(clients)]
    try:
        await wait_for(lambda: len(arrived) == clients, 10)
        assert pooled.checked_out() == 0
        probe = await asyncio.wait_for(pooled.api.get("/skills", params={"limit": 1}), 5)
        site = await asyncio.wait_for(pooled.api.client.get("/registry"), 5)
        assert probe.status_code == 200
        assert site.status_code == 200
    finally:
        gate.set()
        statuses = await finish(tasks)

    assert len(set(statuses)) == 1 and isinstance(statuses[0], int)
    assert pooled.checked_out() == 0


@pytest.mark.asyncio
async def test_slow_markdown_renders_cannot_hold_database_connections(
    pooled: Pooled, monkeypatch: pytest.MonkeyPatch
):
    await publish_site_entry(pooled)
    entered = threading.Event()
    release = threading.Event()
    renders: list[str] = []
    original = public.render_html

    def stalled(text: str, context: public.MarkdownContext):
        renders.append(threading.current_thread().name)
        entered.set()
        assert release.wait(20)
        return original(text, context)

    monkeypatch.setattr(public, "render_html", stalled)
    public.MARKDOWN_CACHE.clear()
    clients = pooled.engine.pool.size() + 10 + 5
    requests = [
        asyncio.create_task(pooled.api.client.get("/registry/skills/site-pool"))
        for _ in range(clients)
    ]
    try:
        await wait_for(entered.is_set, 10)
        await wait_for(lambda: pooled.checked_out() == 0, 10)
        await asyncio.sleep(0.2)
        probe = await asyncio.wait_for(pooled.api.get("/skills", params={"limit": 1}), 5)
        assert probe.status_code == 200
        assert pooled.checked_out() == 0
        assert not any(request.done() for request in requests)
    finally:
        release.set()
        responses = await finish(requests)

    assert [response.status_code for response in responses] == [200] * clients
    assert len(renders) == 1
    assert pooled.checked_out() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("http", ["h11", "httptools"])
async def test_real_clients_that_never_read_a_huge_page_cannot_starve_the_api(
    tight: Pooled, monkeypatch: pytest.MonkeyPatch, http: str
):
    from app.main import app

    page = b"x" * (12 * 1024 * 1024)
    monkeypatch.setattr(
        public, "render", lambda request, name, context, status=200: HTMLResponse(page)
    )
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        lifespan="off",
        log_level="warning",
        log_config=None,
        http=http,
        access_log=False,
        timeout_graceful_shutdown=2,
    )
    server = uvicorn.Server(config)
    serving_task = asyncio.create_task(server.serve())
    await wait_for(lambda: server.started, 10)
    port = server.servers[0].sockets[0].getsockname()[1]
    clients: list[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = []
    try:
        for _ in range(6):
            reader, writer = await asyncio.open_connection("127.0.0.1", port, limit=1024)
            writer.write(b"GET /registry HTTP/1.1\r\nHost: live.test\r\n\r\n")
            await writer.drain()
            clients.append((reader, writer))
        await asyncio.sleep(1.0)
        assert tight.checked_out() == 0
        async with AsyncClient(timeout=5) as http_client:
            probe = await http_client.get(f"http://127.0.0.1:{port}/api/v1/skills?limit=1")
        assert probe.status_code == 200
    finally:
        for _, writer in clients:
            writer.close()
        server.should_exit = True
        await asyncio.wait_for(serving_task, 15)
