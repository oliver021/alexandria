from __future__ import annotations

import asyncio
import random
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx2
import pytest
import uvicorn
from fastapi import FastAPI
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.session import configure_sqlite_engine
from app.mcp import mcp_lifespan, mount_mcp
from app.mcp.context import configure_runtime
from app.models import Base
from app.storage import LocalBlobStore
from tests.helpers.factories import make_actors
from tests.helpers.packages import make_skill_zip
from tests.test_mcp.support import (
    DRAFT,
    PDF,
    Credentials,
    issue_credentials,
    payload_of,
    seed_registry,
    text_of,
    zip_to_base64,
)

STARTUP_TIMEOUT = 60
CALL_TIMEOUT = 60


@dataclass
class LiveServer:
    port: int
    credentials: Credentials
    server: uvicorn.Server

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"


def serve_in_thread(tmp_path: Path, ready: threading.Event, box: dict) -> None:
    async def main() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'live.db'}", connect_args={"timeout": 60}
        )
        configure_sqlite_engine(engine)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        store = LocalBlobStore(tmp_path / "blobs")
        configure_runtime(session_factory=factory, blob_store=store)
        async with factory() as db:
            actors = await make_actors(db)
        await seed_registry(factory, store, actors)
        credentials = await issue_credentials(factory, actors)

        @asynccontextmanager
        async def lifespan(application: FastAPI) -> AsyncIterator[None]:
            async with mcp_lifespan():
                yield

        application = FastAPI(lifespan=lifespan)
        mount_mcp(application)
        config = uvicorn.Config(
            application, host="127.0.0.1", port=0, log_level="warning", lifespan="on"
        )
        server = uvicorn.Server(config)
        task = asyncio.create_task(server.serve())
        while not server.started and not task.done():
            await asyncio.sleep(0.01)
        if task.done():
            await task
        port = server.servers[0].sockets[0].getsockname()[1]
        box["live"] = LiveServer(port=port, credentials=credentials, server=server)
        ready.set()
        await task
        await engine.dispose()

    try:
        asyncio.run(main())
    except Exception as exc:
        box["error"] = exc
        raise
    finally:
        ready.set()


@pytest.fixture
def live(tmp_path: Path) -> Iterator[LiveServer]:
    ready = threading.Event()
    box: dict = {}
    thread = threading.Thread(target=serve_in_thread, args=(tmp_path, ready, box), daemon=True)
    thread.start()
    assert ready.wait(STARTUP_TIMEOUT), "the live server did not start"
    if "error" in box:
        raise box["error"]
    live_server: LiveServer = box["live"]
    yield live_server
    live_server.server.should_exit = True
    thread.join(STARTUP_TIMEOUT)
    assert not thread.is_alive()


@asynccontextmanager
async def connect(live: LiveServer, token: str | None = None, mode: str = "auto"):
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    async with (
        asyncio.timeout(CALL_TIMEOUT),
        httpx2.AsyncClient(headers=headers, timeout=CALL_TIMEOUT) as http,
        Client(streamable_http_client(live.url, http_client=http), mode=mode) as client,
    ):
        yield client


@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_the_sdk_client_works_against_a_real_http_server(live, mode):
    async with connect(live, live.credentials.owner_read, mode) as client:
        tools = await client.list_tools()
        found = payload_of(await client.call_tool("search_skills", {"query": "pdf"}))
        entry = payload_of(await client.call_tool("get_entrypoint", {"name": PDF}))
        draft = payload_of(await client.call_tool("get_skill", {"name": DRAFT}))

    assert len(tools.tools) == 11
    assert [item["name"] for item in found["items"]] == [PDF, "bobs-ocr"]
    assert (entry["name"], entry["version"], entry["kind"]) == (PDF, "1.1.0", "skill")
    assert draft["status"] == "draft"


async def test_anonymous_callers_see_only_public_entries_over_the_wire(live):
    async with connect(live) as client:
        result = await client.call_tool("get_skill", {"name": DRAFT})

    assert result.is_error is True
    assert text_of(result).startswith("NOT_FOUND: ")


async def test_authentication_and_method_errors_are_plain_http_responses(live):
    async with httpx2.AsyncClient(timeout=CALL_TIMEOUT) as http:
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        accept = {"accept": "application/json, text/event-stream"}
        with_slash = await http.post(live.url + "/", json=body, headers=accept)
        without_slash = await http.post(live.url, json=body, headers=accept)
        rejected = await http.post(
            live.url, json=body, headers={**accept, "authorization": "Bearer alx_nope"}
        )
        stream = await http.get(live.url, headers={"accept": "text/event-stream"})

    assert with_slash.status_code == without_slash.status_code == 200
    assert without_slash.history == []
    assert rejected.status_code == 401
    assert rejected.headers["www-authenticate"] == "Bearer"
    assert stream.status_code == 405
    assert stream.headers["allow"] == "POST"


async def test_writers_upload_large_packages_and_readers_are_cut_off(live):
    noise = random.Random(21).randbytes(6 * 1024 * 1024)
    data = make_skill_zip("big-live", "1.0.0", files={"noise.bin": noise})
    arguments = {"content_base64": zip_to_base64(data), "publish": True}
    rejected_arguments = {
        "content_base64": zip_to_base64(make_skill_zip("big-reader", "1.0.0", files={"n": noise}))
    }

    async with connect(live, live.credentials.owner_write, "legacy") as writer:
        uploaded = payload_of(await writer.call_tool("upload_package", arguments))
        stored = payload_of(await writer.call_tool("get_download_info", {"name": "big-live"}))
    outcome: object
    refusal: dict | None = None
    async with httpx2.AsyncClient(timeout=CALL_TIMEOUT) as http:
        try:
            response = await http.post(
                live.url,
                json={
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "tools/call",
                    "params": {"name": "upload_package", "arguments": rejected_arguments},
                },
                headers={
                    "accept": "application/json, text/event-stream",
                    "authorization": f"Bearer {live.credentials.owner_read}",
                },
            )
            outcome = response.status_code
            if outcome == 413:
                refusal = {
                    "type": response.headers["content-type"],
                    "connection": response.headers.get("connection"),
                    "body": response.json(),
                }
        except httpx2.TransportError as error:
            outcome = type(error)
    async with connect(live, live.credentials.owner_write, "legacy") as writer:
        missing = await writer.call_tool("get_skill", {"name": "big-reader"})

    assert uploaded["version"]["size_bytes"] == len(data)
    assert stored["sha256"] == uploaded["version"]["sha256"]
    assert outcome == 413 or (
        isinstance(outcome, type) and issubclass(outcome, httpx2.TransportError)
    )
    if refusal is not None:
        assert refusal["type"] == "application/json"
        assert refusal["body"]["error"]["code"] == "PAYLOAD_TOO_LARGE"
    assert text_of(missing).startswith("NOT_FOUND: ")


async def test_the_whole_workflow_runs_over_real_sockets_for_a_writer(live):
    first = make_skill_zip("live-skill", "1.0.0", files={"docs/a.md": "alpha\n"})
    second = make_skill_zip("live-skill", "1.1.0", files={"docs/a.md": "beta\n"})

    async with connect(live, live.credentials.owner_write) as client:
        await client.call_tool(
            "upload_package", {"content_base64": zip_to_base64(first), "publish": True}
        )
        await client.call_tool("upload_package", {"content_base64": zip_to_base64(second)})
        await client.call_tool("publish_version", {"name": "live-skill", "version": "1.1.0"})
        latest = payload_of(
            await client.call_tool("read_file", {"name": "live-skill", "path": "docs/a.md"})
        )
        await client.call_tool("deprecate_version", {"name": "live-skill", "version": "1.1.0"})
        after = payload_of(
            await client.call_tool("read_file", {"name": "live-skill", "path": "docs/a.md"})
        )
        pinned = payload_of(
            await client.call_tool(
                "read_file", {"name": "live-skill", "path": "docs/a.md", "version": "1.1.0"}
            )
        )

    assert latest["content"] == "beta\n"
    assert after["content"] == "alpha\n"
    assert pinned["content"] == "beta\n"
