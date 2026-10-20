from __future__ import annotations

import hashlib
import inspect
from collections.abc import AsyncIterator

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from app.api.errors import register_exception_handlers
from app.api.responses import ClosingStreamingResponse, HeadRoute, add_head_route
from app.config.settings import settings
from app.services.errors import NotFoundError
from app.storage import LocalBlobStore
from tests.test_api.support import (
    DRAFT_ENTRY,
    HIDDEN_ENTRY,
    PUBLISHED_ENTRY,
    Api,
    Catalog,
    assert_error,
    stream_through_the_app,
)

API = "/api/v1"
DOWNLOAD = f"/skills/{PUBLISHED_ENTRY}/versions/1.0.0/download"
LATEST_DOWNLOAD = f"/skills/{PUBLISHED_ENTRY}/versions/latest/download"
BODY_HEADERS = ("content-type", "content-length")
DOWNLOAD_HEADERS = (
    "content-type",
    "content-length",
    "content-disposition",
    "etag",
    "x-content-sha256",
    "cache-control",
)


def etag_of(data: bytes) -> str:
    return f'"{hashlib.sha256(data).hexdigest()}"'


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/health", "/info"])
async def test_head_answers_with_the_headers_of_get_and_no_body(api: Api, path: str):
    got = await api.get(path)

    head = await api.request("HEAD", path)

    assert head.status_code == got.status_code == 200
    assert head.content == b""
    for name in BODY_HEADERS:
        assert head.headers[name] == got.headers[name], name
    assert int(head.headers["content-length"]) == len(got.content) > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/health", "/info"])
async def test_head_sends_not_one_body_byte_even_at_the_asgi_level(api: Api, path: str):
    got = await api.get(path)

    outcome = await stream_through_the_app(f"{API}{path}", method="HEAD")

    assert outcome.status == 200
    assert (outcome.received, outcome.chunks) == (0, 0)
    assert outcome.headers["content-length"] == str(len(got.content))
    assert outcome.headers["content-type"] == got.headers["content-type"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/health", "/info"])
@pytest.mark.parametrize("actor", ["anonymous", "owner_read"])
async def test_head_on_the_public_routes_needs_no_credential_even_in_private_mode(
    api: Api, monkeypatch: pytest.MonkeyPatch, path: str, actor: str
):
    monkeypatch.setattr(settings.registry, "public_read", False)

    head = await api.request("HEAD", path, actor)

    assert head.status_code == 200


@pytest.mark.asyncio
async def test_head_on_info_follows_the_configuration_like_get(
    api: Api, monkeypatch: pytest.MonkeyPatch
):
    before = await api.request("HEAD", "/info")
    monkeypatch.setattr(
        settings.server, "public_url", "https://a-much-longer-public-url.example.org"
    )

    after = await api.request("HEAD", "/info")
    got = await api.get("/info")

    assert int(after.headers["content-length"]) > int(before.headers["content-length"])
    assert after.headers["content-length"] == got.headers["content-length"]


@pytest.mark.asyncio
async def test_head_on_a_download_has_the_headers_of_get_and_no_body(api: Api, catalog: Catalog):
    got = await api.get(DOWNLOAD)

    head = await api.request("HEAD", DOWNLOAD)

    assert head.status_code == 200
    assert head.content == b""
    for name in DOWNLOAD_HEADERS:
        assert head.headers[name] == got.headers[name], name
    assert head.headers["content-length"] == str(len(catalog.zip_of(PUBLISHED_ENTRY, "1.0.0")))


@pytest.mark.asyncio
async def test_head_on_a_download_sends_not_one_body_byte_at_the_asgi_level(
    api: Api, catalog: Catalog
):
    original = catalog.zip_of(PUBLISHED_ENTRY, "1.0.0")

    outcome = await stream_through_the_app(f"{API}{DOWNLOAD}", method="HEAD")

    assert outcome.status == 200
    assert (outcome.received, outcome.chunks) == (0, 0)
    assert outcome.headers["content-length"] == str(len(original))
    assert outcome.headers["content-type"] == "application/zip"
    assert outcome.headers["etag"] == etag_of(original)


def record_store_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def recording(name: str, original):
        if inspect.iscoroutinefunction(original):

            async def wrapper(self, *args, **kwargs):
                calls.append(name)
                return await original(self, *args, **kwargs)

        else:

            def wrapper(self, *args, **kwargs):
                calls.append(name)
                return original(self, *args, **kwargs)

        return wrapper

    for name in ("get", "open", "stream", "size", "exists"):
        monkeypatch.setattr(LocalBlobStore, name, recording(name, getattr(LocalBlobStore, name)))
    return calls


@pytest.mark.asyncio
async def test_head_on_a_download_never_reads_the_blob_it_only_checks_its_size(
    api: Api, catalog: Catalog, monkeypatch: pytest.MonkeyPatch
):
    calls = record_store_calls(monkeypatch)

    head = await api.request("HEAD", DOWNLOAD)

    assert head.status_code == 200
    assert calls == ["size"]


@pytest.mark.asyncio
async def test_get_does_read_the_blob_so_the_head_test_proves_something(
    api: Api, catalog: Catalog, monkeypatch: pytest.MonkeyPatch
):
    calls = record_store_calls(monkeypatch)

    await api.get(DOWNLOAD)

    assert "stream" in calls


@pytest.mark.asyncio
async def test_head_on_a_download_of_latest_revalidates_like_get(api: Api, catalog: Catalog):
    got = await api.get(LATEST_DOWNLOAD)

    head = await api.request("HEAD", LATEST_DOWNLOAD)

    assert head.headers["cache-control"] == got.headers["cache-control"] == "public, no-cache"
    assert head.headers["etag"] == got.headers["etag"]


@pytest.mark.asyncio
async def test_head_on_a_download_honours_if_none_match_like_get(api: Api, catalog: Catalog):
    etag = etag_of(catalog.zip_of(PUBLISHED_ENTRY, "1.0.0"))
    got = await api.get(DOWNLOAD, headers={"If-None-Match": etag})

    head = await api.request("HEAD", DOWNLOAD, headers={"If-None-Match": etag})
    other = await api.request("HEAD", DOWNLOAD, headers={"If-None-Match": '"other"'})

    assert head.status_code == got.status_code == 304
    assert head.content == b""
    for name in ("etag", "cache-control", "x-content-sha256"):
        assert head.headers[name] == got.headers[name], name
    assert other.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entry", "version", "actor", "status", "cache"),
    [
        (DRAFT_ENTRY, "0.1.0", "anonymous", 404, None),
        (DRAFT_ENTRY, "0.1.0", "stranger", 404, None),
        (DRAFT_ENTRY, "0.1.0", "owner", 200, "private, no-store"),
        (DRAFT_ENTRY, "0.1.0", "owner_read", 200, "private, no-store"),
        (HIDDEN_ENTRY, "1.0.0", "anonymous", 404, None),
        (HIDDEN_ENTRY, "1.0.0", "admin", 200, "private, no-store"),
        (PUBLISHED_ENTRY, "9.9.9", "owner", 404, None),
    ],
)
async def test_head_on_a_download_follows_the_visibility_of_get(
    api: Api, catalog: Catalog, entry: str, version: str, actor: str, status: int, cache
):
    path = f"/skills/{entry}/versions/{version}/download"
    got = await api.get(path, actor)

    head = await api.request("HEAD", path, actor)

    assert head.status_code == got.status_code == status
    if cache is not None:
        assert head.headers["cache-control"] == cache


@pytest.mark.asyncio
async def test_head_on_a_download_is_refused_like_get_when_reading_needs_a_credential(
    api: Api, catalog: Catalog, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings.registry, "public_read", False)

    anonymous = await api.request("HEAD", DOWNLOAD)
    bogus = await api.request("HEAD", DOWNLOAD, headers={"Authorization": "Bearer nope"})
    allowed = await api.request("HEAD", DOWNLOAD, "stranger")

    assert anonymous.status_code == bogus.status_code == 401
    assert anonymous.headers["www-authenticate"] == "Bearer"
    assert allowed.status_code == 200
    assert allowed.headers["cache-control"] == "private, no-store"


@pytest.mark.asyncio
async def test_head_on_a_download_reports_a_lost_blob_like_get(
    api: Api, catalog: Catalog, blob_store: LocalBlobStore
):
    sha = hashlib.sha256(catalog.zip_of(PUBLISHED_ENTRY, "1.0.0")).hexdigest()
    blob_store._path(sha).unlink()

    head = await api.request("HEAD", DOWNLOAD)
    got = await api.get(DOWNLOAD)

    assert head.status_code == got.status_code == 500
    assert_error(got, 500, "STORAGE_ERROR")


@pytest.mark.asyncio
async def test_head_on_a_download_notices_a_blob_of_the_wrong_size(
    api: Api, catalog: Catalog, blob_store: LocalBlobStore
):
    sha = hashlib.sha256(catalog.zip_of(PUBLISHED_ENTRY, "1.0.0")).hexdigest()
    path = blob_store._path(sha)
    path.write_bytes(path.read_bytes()[:-7])

    head = await api.request("HEAD", DOWNLOAD)

    assert head.status_code == 500


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/skills",
        f"/skills/{PUBLISHED_ENTRY}",
        f"/skills/{PUBLISHED_ENTRY}/versions",
        f"/skills/{PUBLISHED_ENTRY}/versions/1.0.0",
        f"/skills/{PUBLISHED_ENTRY}/versions/1.0.0/entrypoint",
        f"/skills/{PUBLISHED_ENTRY}/versions/1.0.0/files",
        f"/skills/{PUBLISHED_ENTRY}/versions/1.0.0/files/docs/guide.md",
        f"/skills/{PUBLISHED_ENTRY}/versions/1.0.0/raw/docs/guide.md",
        "/tags",
        "/categories",
        "/auth/me",
        "/tokens",
        "/users",
    ],
)
async def test_head_is_offered_only_on_health_info_and_downloads(
    api: Api, catalog: Catalog, path: str
):
    response = await api.request("HEAD", path, "owner")

    assert response.status_code == 405


@pytest.mark.asyncio
async def test_a_wrong_method_on_the_head_routes_still_names_get_in_allow(api: Api):
    response = await api.request("DELETE", "/health")

    assert_error(response, 405, "METHOD_NOT_ALLOWED")
    assert "GET" in response.headers["allow"]


def build_app() -> tuple[FastAPI, list[str]]:
    events: list[str] = []
    router = APIRouter()

    async def chunks() -> AsyncIterator[bytes]:
        events.append("started")
        try:
            yield b"payload"
        finally:
            events.append("closed")

    async def stream() -> ClosingStreamingResponse:
        return ClosingStreamingResponse(
            chunks(), media_type="application/zip", headers={"Content-Length": "7", "X-Extra": "1"}
        )

    async def unsized() -> ClosingStreamingResponse:
        return ClosingStreamingResponse(chunks(), media_type="text/plain")

    async def plain() -> JSONResponse:
        return JSONResponse({"a": 1}, headers={"X-Plain": "yes"})

    async def missing() -> dict[str, str]:
        raise NotFoundError("nothing here")

    routes = (("/stream", stream), ("/unsized", unsized), ("/plain", plain), ("/missing", missing))
    for path, endpoint in routes:
        router.add_api_route(path, endpoint, methods=["GET"])
        add_head_route(router, path, endpoint)
    application = FastAPI()
    application.include_router(router)
    register_exception_handlers(application)
    return application, events


async def local_client(application: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=application), base_url="http://test")


@pytest.mark.asyncio
async def test_a_head_route_closes_a_stream_it_never_started():
    application, events = build_app()

    async with await local_client(application) as client:
        head = await client.head("/stream")
        got = await client.get("/stream")

    assert head.status_code == got.status_code == 200
    assert head.content == b"" and got.content == b"payload"
    assert head.headers["content-length"] == got.headers["content-length"] == "7"
    assert head.headers["content-type"] == got.headers["content-type"] == "application/zip"
    assert head.headers["x-extra"] == "1"
    assert events == ["started", "closed"]


@pytest.mark.asyncio
async def test_a_head_route_does_not_invent_a_length_the_response_never_declared():
    application, events = build_app()

    async with await local_client(application) as client:
        head = await client.head("/unsized")
        got = await client.get("/unsized")

    assert "content-length" not in head.headers
    assert head.headers["content-type"].startswith("text/plain")
    assert head.content == b"" and got.content == b"payload"
    assert events == ["started", "closed"]


@pytest.mark.asyncio
async def test_a_head_route_keeps_the_status_and_headers_of_an_ordinary_response():
    application, _ = build_app()

    async with await local_client(application) as client:
        head = await client.head("/plain")
        got = await client.get("/plain")

    assert head.status_code == got.status_code == 200
    assert head.headers["x-plain"] == "yes"
    assert head.headers["content-length"] == got.headers["content-length"]
    assert head.content == b""


@pytest.mark.asyncio
async def test_a_head_route_lets_domain_errors_through_to_the_handlers():
    application, _ = build_app()

    async with await local_client(application) as client:
        head = await client.head("/missing")
        got = await client.get("/missing")

    assert head.status_code == got.status_code == 404
    assert got.json()["error"]["code"] == "NOT_FOUND"


def test_the_head_helper_registers_a_hidden_head_only_route():
    router = APIRouter()

    async def endpoint() -> dict[str, int]:
        return {}

    add_head_route(router, "/thing", endpoint)

    (route,) = router.routes
    assert route.methods == {"HEAD"}
    assert route.include_in_schema is False
    assert isinstance(route, HeadRoute)
    assert route.name == "head_endpoint"
