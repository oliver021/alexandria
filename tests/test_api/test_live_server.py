from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import zipfile
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_asyncio
import uvicorn
from httpx import AsyncClient

from app.api.body_limit import MULTIPART_ALLOWANCE
from app.config.settings import settings
from tests.helpers.packages import make_skill_zip, make_zeros_zip
from tests.test_api.support import Api

BOUNDARY = "live-boundary"
TIMEOUT = 10.0
HTTP_IMPLEMENTATIONS = ["h11", "httptools"]


@dataclass
class Live:
    port: int
    api: Api

    async def connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.open_connection("127.0.0.1", self.port)

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}/api/v1{path}"


@pytest_asyncio.fixture(params=HTTP_IMPLEMENTATIONS)
async def live(request: pytest.FixtureRequest, client: AsyncClient, tokens) -> AsyncIterator[Live]:
    from app.main import app

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        lifespan="off",
        log_level="warning",
        log_config=None,
        http=request.param,
        access_log=False,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.02)
    assert server.started, "the live server did not start"
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield Live(port, Api(client, tokens))
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, TIMEOUT)


def request_head(
    method: str,
    path: str,
    headers: dict[str, str],
) -> bytes:
    lines = [f"{method} /api/v1{path} HTTP/1.1", "Host: live.test"]
    lines += [f"{name}: {value}" for name, value in headers.items()]
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


async def read_response(reader: asyncio.StreamReader) -> tuple[int, dict[str, str], bytes]:
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), TIMEOUT)
    status_line, *header_lines = head.decode("latin-1").split("\r\n")
    headers = {}
    for line in header_lines:
        if ":" in line:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    body = await asyncio.wait_for(reader.readexactly(length), TIMEOUT) if length else b""
    return int(status_line.split()[1]), headers, body


def chunked(payload: bytes) -> bytes:
    return f"{len(payload):x}\r\n".encode() + payload + b"\r\n"


def multipart_head(filename: str = "p.zip") -> bytes:
    return (
        f"--{BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/zip\r\n\r\n"
    ).encode()


def multipart_headers(live: Live, actor: str | None = "owner") -> dict[str, str]:
    headers = {"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"}
    headers.update(live.api.headers(actor))
    return headers


async def close(writer: asyncio.StreamWriter) -> None:
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), TIMEOUT)
    except (ConnectionError, OSError):
        pass


@pytest.mark.asyncio
async def test_a_package_uploaded_and_downloaded_over_a_real_connection_is_intact(live: Live):
    blob = random.Random(11).randbytes(700 * 1024)
    data = make_skill_zip("wire-skill", "1.0.0", files={"payload.bin": blob})
    async with AsyncClient(timeout=TIMEOUT) as http:
        uploaded = await http.post(
            live.url("/packages"),
            headers=live.api.headers("owner"),
            files={"file": ("p.zip", data, "application/zip")},
            data={"publish": "true"},
        )
        downloaded = await http.get(live.url("/skills/wire-skill/versions/latest/download"))
        revalidated = await http.get(
            live.url("/skills/wire-skill/versions/latest/download"),
            headers={"If-None-Match": downloaded.headers["etag"]},
        )
        pinned = await http.get(live.url("/skills/wire-skill/versions/1.0.0/download"))

    assert uploaded.status_code == 201
    assert downloaded.status_code == 200
    assert downloaded.content == data
    assert downloaded.headers["content-length"] == str(len(data))
    assert downloaded.headers["etag"] == f'"{hashlib.sha256(data).hexdigest()}"'
    assert downloaded.headers["cache-control"] == "public, no-cache"
    assert revalidated.status_code == 304
    assert revalidated.content == b""
    assert revalidated.headers["cache-control"] == "public, no-cache"
    assert pinned.content == data
    assert pinned.headers["cache-control"] == "public, immutable"


@pytest.mark.asyncio
async def test_a_huge_declared_length_is_refused_before_a_single_body_byte_is_sent(live: Live):
    reader, writer = await live.connect()
    headers = {
        "Content-Type": f"multipart/form-data; boundary={BOUNDARY}",
        "Content-Length": str(10**11),
        **live.api.headers("owner"),
    }
    writer.write(request_head("POST", "/packages", headers))
    await writer.drain()

    status, response_headers, body = await read_response(reader)

    assert status == 413
    assert response_headers["connection"] == "close"
    assert json.loads(body)["error"]["code"] == "PAYLOAD_TOO_LARGE"
    assert await asyncio.wait_for(reader.read(), TIMEOUT) == b""
    await close(writer)


@pytest.mark.asyncio
async def test_an_endless_chunked_upload_is_cut_off_long_before_it_ends(
    live: Live, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings.registry.limits, "max_package_bytes", 100_000)
    limit = 100_000 + MULTIPART_ALLOWANCE
    reader, writer = await live.connect()
    headers = {
        "Transfer-Encoding": "chunked",
        "Content-Type": f"multipart/form-data; boundary={BOUNDARY}",
        **live.api.headers("owner"),
    }
    writer.write(request_head("POST", "/packages", headers))
    writer.write(chunked(multipart_head()))
    sent = 0
    piece = chunked(b"\x00" * 64 * 1024)
    total_pieces = 2000

    async def pump() -> None:
        nonlocal sent
        try:
            for _ in range(total_pieces):
                writer.write(piece)
                sent += len(piece)
                await writer.drain()
        except (ConnectionError, OSError):
            pass

    pumping = asyncio.create_task(pump())
    try:
        status, _, body = await read_response(reader)
    finally:
        pumping.cancel()
        await asyncio.gather(pumping, return_exceptions=True)

    assert status == 413
    assert json.loads(body)["error"]["code"] == "PAYLOAD_TOO_LARGE"
    assert sent < total_pieces * len(piece) / 4
    assert sent >= limit
    await close(writer)


@pytest.mark.asyncio
@pytest.mark.parametrize(("actor", "status"), [("anonymous", 401), ("owner_read", 403)])
async def test_an_unauthorized_upload_is_refused_without_waiting_for_its_body(
    live: Live, actor: str, status: int
):
    reader, writer = await live.connect()
    headers = {**multipart_headers(live, actor), "Content-Length": "5000000"}
    writer.write(request_head("POST", "/packages", headers))
    await writer.drain()

    code, response_headers, body = await read_response(reader)

    assert code == status
    assert json.loads(body)["error"]["code"] in {"UNAUTHORIZED", "FORBIDDEN"}
    if status == 401:
        assert response_headers["www-authenticate"] == "Bearer"
    await close(writer)


@pytest.mark.asyncio
async def test_an_upload_abandoned_by_the_client_is_a_quiet_event(
    live: Live, caplog: pytest.LogCaptureFixture
):
    with caplog.at_level(logging.INFO):
        _, writer = await live.connect()
        headers = {**multipart_headers(live), "Content-Length": "4000000"}
        writer.write(request_head("POST", "/packages", headers))
        writer.write(multipart_head() + b"PK" * 5000)
        await writer.drain()
        await asyncio.sleep(0.2)
        await close(writer)
        for _ in range(100):
            if any("Client disconnected" in record.getMessage() for record in caplog.records):
                break
            await asyncio.sleep(0.05)

    assert any("Client disconnected" in record.getMessage() for record in caplog.records)
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
    health = await asyncio.wait_for(live.connect(), TIMEOUT)
    health[1].write(request_head("GET", "/health", {"Connection": "close"}))
    await health[1].drain()
    status, _, _ = await read_response(health[0])
    assert status == 200
    await close(health[1])


@pytest.mark.asyncio
async def test_a_download_abandoned_by_the_client_is_a_quiet_event(
    live: Live, caplog: pytest.LogCaptureFixture
):
    blob = random.Random(12).randbytes(6 * 1024 * 1024)
    data = make_skill_zip("abandoned-skill", "1.0.0", files={"payload.bin": blob})
    async with AsyncClient(timeout=TIMEOUT) as http:
        uploaded = await http.post(
            live.url("/packages"),
            headers=live.api.headers("owner"),
            files={"file": ("p.zip", data, "application/zip")},
            data={"publish": "true"},
        )
    assert uploaded.status_code == 201

    with caplog.at_level(logging.INFO):
        reader, writer = await live.connect()
        writer.write(
            request_head(
                "GET", "/skills/abandoned-skill/versions/1.0.0/download", {"Connection": "close"}
            )
        )
        await writer.drain()
        await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), TIMEOUT)
        await asyncio.wait_for(reader.read(1024), TIMEOUT)
        await close(writer)
        await asyncio.sleep(0.5)

    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
    async with AsyncClient(timeout=TIMEOUT) as http:
        again = await http.get(live.url("/skills/abandoned-skill/versions/1.0.0/download"))
    assert again.content == data


@pytest.mark.asyncio
async def test_error_responses_over_a_real_connection_keep_their_format(live: Live):
    async with AsyncClient(timeout=TIMEOUT) as http:
        missing = await http.get(live.url("/skills/nobody"))
        invalid = await http.get(live.url("/skills"), headers={"Authorization": "Basic abc"})
        invalid_json = await http.post(
            live.url("/auth/login"), content=b"{nope", headers={"Content-Type": "application/json"}
        )

    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "NOT_FOUND"
    assert invalid.status_code == 401
    assert invalid.headers["www-authenticate"] == "Bearer"
    assert invalid_json.status_code == 422


def open_blob_files(root: Path) -> list[str]:
    opened = []
    for descriptor in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{descriptor}")
        except OSError:
            continue
        if target.startswith(str(root)):
            opened.append(target)
    return opened


async def wait_until(condition: Callable[[], bool], timeout: float = TIMEOUT) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.02)
    return condition()


async def publish(live: Live, name: str, data: bytes) -> None:
    async with AsyncClient(timeout=TIMEOUT) as http:
        uploaded = await http.post(
            live.url("/packages"),
            headers=live.api.headers("owner"),
            files={"file": (f"{name}.zip", data, "application/zip")},
            data={"publish": "true"},
        )
    assert uploaded.status_code == 201


@pytest.mark.asyncio
async def test_a_raw_file_over_a_real_connection_is_streamed_intact_with_the_manifest_length(
    live: Live,
):
    payload = random.Random(13).randbytes(700 * 1024)
    notes = "plain text line\n" * 500
    data = make_skill_zip(
        "wire-raw",
        "1.0.0",
        files={"payload.bin": payload, "notes.txt": notes},
        compression=zipfile.ZIP_STORED,
    )
    await publish(live, "wire-raw", data)

    async with AsyncClient(timeout=TIMEOUT) as http:
        binary = await http.get(live.url("/skills/wire-raw/versions/1.0.0/raw/payload.bin"))
        text = await http.get(live.url("/skills/wire-raw/versions/1.0.0/raw/notes.txt"))
        missing = await http.get(live.url("/skills/wire-raw/versions/1.0.0/raw/nope.bin"))

    assert binary.status_code == 200
    assert binary.content == payload
    assert binary.headers["content-length"] == str(len(payload))
    assert "transfer-encoding" not in binary.headers
    assert binary.headers["content-type"] == "application/octet-stream"
    assert binary.headers["content-disposition"] == 'attachment; filename="payload.bin"'
    assert text.status_code == 200 and text.text == notes
    assert text.headers["content-length"] == str(len(notes))
    assert text.headers["content-disposition"] == "inline"
    for response in (binary, text):
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_a_corrupted_raw_file_answers_a_complete_json_500_over_a_real_connection(
    live: Live, blob_store
):
    payload = random.Random(14).randbytes(300 * 1024)
    data = make_skill_zip(
        "wire-damaged", "1.0.0", files={"payload.bin": payload}, compression=zipfile.ZIP_STORED
    )
    await publish(live, "wire-damaged", data)
    stored = blob_store._path(hashlib.sha256(data).hexdigest())
    damaged = bytearray(stored.read_bytes())
    damaged[len(damaged) // 2] ^= 0xFF
    stored.write_bytes(bytes(damaged))

    reader, writer = await live.connect()
    writer.write(
        request_head(
            "GET", "/skills/wire-damaged/versions/1.0.0/raw/payload.bin", {"Connection": "close"}
        )
    )
    await writer.drain()
    status, headers, body = await read_response(reader)

    assert status == 500
    assert headers["content-type"].startswith("application/json")
    assert json.loads(body)["error"]["code"] == "STORAGE_ERROR"
    assert payload[:64] not in body
    await close(writer)


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="needs /proc/self/fd")
@pytest.mark.asyncio
async def test_a_client_that_hangs_up_during_a_raw_download_leaves_no_file_open(
    live: Live, blob_store, caplog: pytest.LogCaptureFixture
):
    await publish(live, "hangup-raw", make_zeros_zip(48 * 1024 * 1024, name="hangup-raw"))
    assert open_blob_files(blob_store.root) == []

    with caplog.at_level(logging.INFO):
        reader, writer = await live.connect()
        writer.write(
            request_head(
                "GET",
                "/skills/hangup-raw/versions/1.0.0/raw/payload.bin",
                {"Connection": "close"},
            )
        )
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), TIMEOUT)
        assert head.startswith(b"HTTP/1.1 200")
        await asyncio.wait_for(reader.readexactly(64 * 1024), TIMEOUT)
        assert len(open_blob_files(blob_store.root)) == 1
        writer.transport.abort()

        released = await wait_until(lambda: open_blob_files(blob_store.root) == [])

    assert released, open_blob_files(blob_store.root)
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
    async with AsyncClient(timeout=TIMEOUT) as http:
        again = await http.get(live.url("/skills/hangup-raw/versions/1.0.0/raw/payload.bin"))
    assert again.status_code == 200
    assert len(again.content) == 48 * 1024 * 1024
    assert await wait_until(lambda: open_blob_files(blob_store.root) == [])


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="needs /proc/self/fd")
@pytest.mark.asyncio
async def test_a_client_that_hangs_up_during_a_package_download_leaves_no_file_open(
    live: Live, blob_store, caplog: pytest.LogCaptureFixture
):
    data = make_skill_zip(
        "hangup-zip",
        "1.0.0",
        files={"payload.bin": os.urandom(20 * 1024 * 1024)},
        compression=zipfile.ZIP_STORED,
    )
    await publish(live, "hangup-zip", data)

    with caplog.at_level(logging.INFO):
        reader, writer = await live.connect()
        writer.write(
            request_head(
                "GET", "/skills/hangup-zip/versions/1.0.0/download", {"Connection": "close"}
            )
        )
        await writer.drain()
        await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), TIMEOUT)
        await asyncio.wait_for(reader.readexactly(64 * 1024), TIMEOUT)
        assert len(open_blob_files(blob_store.root)) == 1
        writer.transport.abort()

        released = await wait_until(lambda: open_blob_files(blob_store.root) == [])

    assert released, open_blob_files(blob_store.root)
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


async def read_head_response(reader: asyncio.StreamReader) -> tuple[int, dict[str, str]]:
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), TIMEOUT)
    status_line, *header_lines = head.decode("latin-1").split("\r\n")
    headers = {}
    for line in header_lines:
        if ":" in line:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
    return int(status_line.split()[1]), headers


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/health", "/info"])
async def test_head_sends_the_headers_of_get_and_no_body_over_a_real_connection(
    live: Live, path: str
):
    async with AsyncClient(timeout=TIMEOUT) as http:
        got = await http.get(live.url(path))
    reader, writer = await live.connect()
    try:
        writer.write(request_head("HEAD", path, {}))
        await writer.drain()
        status, headers = await read_head_response(reader)
        writer.write(request_head("GET", "/health", {}))
        await writer.drain()
        next_status, _, next_body = await read_response(reader)
    finally:
        await close(writer)

    assert status == 200
    assert headers["content-length"] == str(len(got.content))
    assert headers["content-type"] == got.headers["content-type"]
    assert (next_status, json.loads(next_body)) == (200, {"status": "ok"})


@pytest.mark.asyncio
async def test_head_on_a_download_announces_the_package_and_sends_nothing_over_a_real_connection(
    live: Live,
):
    data = make_skill_zip("wire-head", "1.0.0", files={"payload.bin": os.urandom(300 * 1024)})
    await publish(live, "wire-head", data)
    path = "/skills/wire-head/versions/1.0.0/download"
    reader, writer = await live.connect()
    try:
        writer.write(request_head("HEAD", path, {}))
        await writer.drain()
        status, headers = await read_head_response(reader)
        writer.write(request_head("GET", path, {}))
        await writer.drain()
        next_status, next_headers, next_body = await read_response(reader)
    finally:
        await close(writer)

    assert status == next_status == 200
    assert headers["content-length"] == next_headers["content-length"] == str(len(data))
    for name in ("etag", "x-content-sha256", "cache-control", "content-type"):
        assert headers[name] == next_headers[name], name
    assert next_body == data
