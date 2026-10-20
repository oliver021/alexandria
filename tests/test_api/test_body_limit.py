from __future__ import annotations

import base64
import json
import logging
import tempfile
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from starlette.datastructures import UploadFile

from app.api import body_limit
from app.api.body_limit import (
    DEFAULT_BODY_LIMIT,
    MULTIPART_ALLOWANCE,
    BodyLimitMiddleware,
    RequestBodyTooLarge,
    body_limit_for,
    declared_length,
    route_path,
)
from app.api.packages import read_at_most
from app.config.settings import LimitsSettings, settings
from app.services import package_service as package_service_module
from app.services.package_service import base64_length
from tests.helpers.packages import make_skill_zip
from tests.test_api.support import Api, assert_error, assert_no_leak

BOUNDARY = "limit-test-boundary"
MULTIPART = {"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"}
ONE_MIB = 1024 * 1024


@dataclass
class Outcome:
    status: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    consumed: int = 0
    downstream_called: bool = False
    downstream_received: int = 0
    sent: list[dict[str, Any]] = field(default_factory=list)


def reading_app(outcome: Outcome):
    async def app(scope, receive, send):
        outcome.downstream_called = True
        total = 0
        while True:
            message = await receive()
            total += len(message.get("body", b""))
            if not message.get("more_body"):
                break
        outcome.downstream_received = total
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    return app


async def drive(
    app: Callable,
    *,
    headers: list[tuple[str, str]] | None = None,
    chunks: list[bytes] | None = None,
    method: str = "POST",
    path: str = "/probe",
    scope_type: str = "http",
    root_path: str = "",
    outcome: Outcome | None = None,
) -> Outcome:
    outcome = outcome or Outcome()
    pending = list(chunks or [])

    async def receive():
        if pending:
            outcome.consumed += 1
            return {"type": "http.request", "body": pending.pop(0), "more_body": True}
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        outcome.sent.append(message)
        if message["type"] == "http.response.start":
            outcome.status = message["status"]
            outcome.headers = {k.decode(): v.decode() for k, v in message["headers"]}
        elif message["type"] == "http.response.body":
            outcome.body += message.get("body", b"")

    scope = {
        "type": scope_type,
        "method": method,
        "path": path,
        "root_path": root_path,
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or [])],
    }
    await app(scope, receive, send)
    return outcome


def middleware(limit: int, outcome: Outcome) -> BodyLimitMiddleware:
    return BodyLimitMiddleware(reading_app(outcome), policy=lambda method, path: limit)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/v1/tokens"),
        ("POST", "/api/v1/auth/login"),
        ("GET", "/api/v1/packages"),
        ("PUT", "/api/v1/packages"),
        ("POST", "/api/v1/packages/extra"),
        ("POST", "/mcpx"),
        ("GET", "/mcp"),
        ("POST", "/packages"),
        ("GET", "/"),
    ],
)
def test_everything_but_the_upload_gets_the_small_limit(method: str, path: str):
    assert body_limit_for(method, path) == DEFAULT_BODY_LIMIT == ONE_MIB


def test_the_upload_limit_follows_the_package_limit_plus_a_multipart_allowance(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings.registry.limits, "max_package_bytes", 5000)

    assert body_limit_for("POST", "/api/v1/packages") == 5000 + MULTIPART_ALLOWANCE
    monkeypatch.setattr(settings.registry.limits, "max_package_bytes", 70_000)
    assert body_limit_for("POST", "/api/v1/packages") == 70_000 + MULTIPART_ALLOWANCE


@pytest.mark.parametrize("path", ["/api/v1/packages", "/api/v1/packages/"])
def test_the_upload_path_with_or_without_a_trailing_slash_gets_the_upload_limit(
    path: str, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings.registry.limits, "max_package_bytes", 9000)

    assert body_limit_for("POST", path) == 9000 + MULTIPART_ALLOWANCE


@pytest.mark.parametrize("path", ["/mcp", "/mcp/", "/mcp/anything"])
def test_the_mcp_endpoint_is_allowed_a_base64_encoded_package(
    path: str, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings.registry.limits, "max_package_bytes", 3 * ONE_MIB)

    assert body_limit_for("POST", path) == 4 * ONE_MIB + MULTIPART_ALLOWANCE


@pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
def test_the_mcp_endpoint_is_never_limited_below_the_small_default(
    path: str, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings.registry.limits, "max_package_bytes", 3000)

    assert body_limit_for("POST", path) == DEFAULT_BODY_LIMIT


def test_the_mcp_limit_is_derived_from_the_single_encoded_length_helper(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(package_service_module, "base64_length", lambda size: 7_000_000 + size)
    monkeypatch.setattr(settings.registry.limits, "max_package_bytes", 1000)

    assert body_limit_for("POST", "/mcp") == 7_001_000 + MULTIPART_ALLOWANCE
    assert body_limit_for("POST", "/api/v1/packages") == 1000 + MULTIPART_ALLOWANCE


def test_the_module_keeps_no_copy_of_the_encoded_length_formula():
    assert not hasattr(body_limit, "base64_length")


@pytest.mark.parametrize(
    ("size", "encoded"), [(0, 0), (1, 4), (2, 4), (3, 4), (4, 8), (25 * ONE_MIB, 34952536)]
)
def test_base64_length_matches_the_real_encoding(size: int, encoded: int):
    assert base64_length(size) == encoded
    assert len(base64.b64encode(b"\x00" * min(size, 50))) == base64_length(min(size, 50))


def test_the_default_package_limit_is_the_contract_value_and_the_allowance_is_generous():
    assert LimitsSettings().max_package_bytes == 25 * ONE_MIB
    assert 16 * 1024 <= MULTIPART_ALLOWANCE <= ONE_MIB


@pytest.mark.parametrize(
    ("path", "root", "expected"),
    [
        ("/api/v1/packages", "", "/api/v1/packages"),
        ("/sub/api/v1/packages", "/sub", "/api/v1/packages"),
        ("/sub", "/sub", "/"),
        ("/other/api", "/sub", "/other/api"),
    ],
)
def test_the_mount_prefix_is_not_part_of_the_route_path(path, root, expected):
    assert route_path({"path": path, "root_path": root}) == expected


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ([(b"content-length", b"123")], 123),
        ([(b"content-length", b"0")], 0),
        ([(b"content-length", b"abc")], None),
        ([(b"content-length", b"")], None),
        ([(b"content-type", b"x")], None),
        ([], None),
    ],
)
def test_the_declared_length_is_read_when_it_is_a_number(headers, expected):
    assert declared_length({"headers": headers}) == expected


@pytest.mark.asyncio
async def test_a_declared_length_over_the_limit_is_rejected_without_calling_the_app():
    outcome = Outcome()

    await drive(
        middleware(100, outcome),
        headers=[("content-length", "101")],
        chunks=[b"x" * 10],
        outcome=outcome,
    )

    assert outcome.status == 413
    assert outcome.downstream_called is False
    assert outcome.consumed == 0
    assert json.loads(outcome.body)["error"]["code"] == "PAYLOAD_TOO_LARGE"
    assert outcome.headers["content-type"] == "application/json"
    assert outcome.headers["connection"] == "close"


@pytest.mark.asyncio
async def test_a_declared_length_far_beyond_the_limit_is_rejected_the_same_way():
    outcome = Outcome()

    await drive(
        middleware(100, outcome), headers=[("content-length", str(10**15))], outcome=outcome
    )

    assert outcome.status == 413
    assert outcome.downstream_called is False


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "DELETE", "POST", "PUT"])
async def test_the_declared_length_is_checked_for_every_method(method: str):
    outcome = Outcome()

    await drive(
        middleware(10, outcome),
        method=method,
        headers=[("content-length", "11")],
        outcome=outcome,
    )

    assert outcome.status == 413


@pytest.mark.asyncio
async def test_a_body_exactly_at_the_limit_goes_through_by_declared_length_and_by_counting():
    declared = Outcome()
    await drive(
        middleware(100, declared),
        headers=[("content-length", "100")],
        chunks=[b"x" * 60, b"x" * 40],
        outcome=declared,
    )
    undeclared = Outcome()
    await drive(middleware(100, undeclared), chunks=[b"x" * 60, b"x" * 40], outcome=undeclared)

    for outcome in (declared, undeclared):
        assert outcome.status == 200
        assert outcome.downstream_received == 100
        assert outcome.body == b"ok"


@pytest.mark.asyncio
async def test_one_byte_over_the_limit_is_rejected_by_counting_when_nothing_is_declared():
    outcome = Outcome()

    await drive(middleware(100, outcome), chunks=[b"x" * 60, b"x" * 41], outcome=outcome)

    assert outcome.status == 413
    assert json.loads(outcome.body)["error"]["code"] == "PAYLOAD_TOO_LARGE"
    assert outcome.downstream_received == 0


@pytest.mark.asyncio
async def test_an_endless_body_is_cut_off_at_the_limit_without_reading_the_rest():
    outcome = Outcome()
    chunks = [b"x" * 10 for _ in range(1000)]

    await drive(middleware(100, outcome), chunks=chunks, outcome=outcome)

    assert outcome.status == 413
    assert outcome.consumed == 11
    assert len(chunks) == 1000


@pytest.mark.asyncio
async def test_a_body_larger_than_it_claims_is_rejected_by_counting():
    outcome = Outcome()

    await drive(
        middleware(100, outcome),
        headers=[("content-length", "5")],
        chunks=[b"x" * 50, b"x" * 50, b"x" * 50],
        outcome=outcome,
    )

    assert outcome.status == 413
    assert outcome.consumed == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("claimed", ["abc", "", "-5", "1e3", "٣"])
async def test_a_content_length_that_is_not_a_plain_number_does_not_disable_counting(claimed):
    outcome = Outcome()

    await drive(
        middleware(100, outcome),
        headers=[("content-length", claimed)],
        chunks=[b"x" * 60, b"x" * 60],
        outcome=outcome,
    )

    assert outcome.status == 413


@pytest.mark.asyncio
async def test_requests_without_a_body_pass_untouched():
    outcome = Outcome()

    await drive(middleware(100, outcome), method="GET", outcome=outcome)

    assert outcome.status == 200
    assert outcome.downstream_received == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("scope_type", ["lifespan", "websocket"])
async def test_only_http_requests_are_inspected(scope_type: str):
    outcome = Outcome()
    seen: list[str] = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    await drive(
        BodyLimitMiddleware(inner, policy=lambda method, path: 1),
        scope_type=scope_type,
        headers=[("content-length", "999999")],
        outcome=outcome,
    )

    assert seen == [scope_type]
    assert outcome.sent == []


@pytest.mark.asyncio
async def test_the_limit_is_chosen_per_method_and_path():
    seen: list[tuple[str, str]] = []

    def policy(method: str, path: str) -> int:
        seen.append((method, path))
        return 10 if path == "/small" else 1000

    outcome = Outcome()
    app = BodyLimitMiddleware(reading_app(outcome), policy=policy)

    small = await drive(app, path="/small", chunks=[b"x" * 11], outcome=Outcome())
    big = await drive(app, path="/big", chunks=[b"x" * 11], outcome=outcome)

    assert small.status == 413
    assert big.status == 200
    assert seen == [("POST", "/small"), ("POST", "/big")]


@pytest.mark.asyncio
async def test_the_mount_prefix_is_removed_before_the_policy_is_asked():
    seen: list[str] = []

    def policy(method: str, path: str) -> int:
        seen.append(path)
        return 10

    outcome = Outcome()
    app = BodyLimitMiddleware(reading_app(outcome), policy=policy)

    await drive(app, path="/mount/x", root_path="/mount", outcome=outcome)

    assert seen == ["/x"]


@pytest.mark.asyncio
async def test_once_the_response_has_started_the_overflow_is_not_hidden():
    async def late_reader(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        while True:
            message = await receive()
            if not message.get("more_body"):
                break
        await send({"type": "http.response.body", "body": b"late"})

    app = BodyLimitMiddleware(late_reader, policy=lambda method, path: 10)
    outcome = Outcome()

    with pytest.raises(RequestBodyTooLarge):
        await drive(app, chunks=[b"x" * 20], outcome=outcome)

    assert [m["type"] for m in outcome.sent] == ["http.response.start"]


@pytest.mark.asyncio
async def test_the_exception_the_app_sees_is_a_413_http_exception():
    captured: list[Exception] = []

    async def catching(scope, receive, send):
        try:
            await receive()
        except Exception as exc:
            captured.append(exc)
            raise

    app = BodyLimitMiddleware(catching, policy=lambda method, path: 5)

    await drive(app, chunks=[b"x" * 20], outcome=Outcome())

    assert isinstance(captured[0], RequestBodyTooLarge)
    assert captured[0].status_code == 413
    assert captured[0].limit == 5
    assert captured[0].headers == {"Connection": "close"}


def json_padded_to(size: int, name: str = "padded") -> bytes:
    body = json.dumps({"name": name}).encode()
    return body + b" " * (size - len(body))


async def endless(chunk: bytes, total_chunks: int, consumed: list[int]) -> AsyncIterator[bytes]:
    for _ in range(total_chunks):
        consumed.append(1)
        yield chunk


@pytest.mark.asyncio
async def test_a_json_body_of_exactly_one_mebibyte_still_reaches_the_route(api: Api):
    body = json_padded_to(ONE_MIB)
    assert len(body) == ONE_MIB

    response = await api.post(
        "/tokens", "owner", content=body, headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 201


@pytest.mark.asyncio
async def test_one_more_byte_is_a_413(api: Api):
    body = json_padded_to(ONE_MIB + 1)

    response = await api.post(
        "/tokens", "owner", content=body, headers={"Content-Type": "application/json"}
    )

    error = assert_error(response, 413, "PAYLOAD_TOO_LARGE")
    assert str(ONE_MIB) in error["message"]
    assert response.headers["connection"] == "close"


@pytest.mark.asyncio
async def test_a_huge_declared_length_is_a_413_before_authentication(api: Api):
    for actor in ("anonymous", "owner", "owner_read"):
        response = await api.post(
            "/tokens",
            actor,
            content=b'{"name": "x"}',
            headers={"Content-Type": "application/json", "Content-Length": str(10**12)},
        )
        assert_error(response, 413, "PAYLOAD_TOO_LARGE")


@pytest.mark.asyncio
async def test_a_chunked_json_body_without_length_is_cut_off_and_not_fully_read(api: Api):
    consumed: list[int] = []

    response = await api.post(
        "/tokens",
        "owner",
        content=endless(b" " * 65536, 64, consumed),
        headers={"Content-Type": "application/json"},
    )

    assert_error(response, 413, "PAYLOAD_TOO_LARGE")
    assert len(consumed) <= 18
    assert len(consumed) < 64


@pytest.mark.asyncio
async def test_a_body_that_lies_about_its_length_is_still_a_413(api: Api):
    response = await api.post(
        "/tokens",
        "owner",
        content=b" " * (ONE_MIB + 10),
        headers={"Content-Type": "application/json", "Content-Length": "12"},
    )

    assert_error(response, 413, "PAYLOAD_TOO_LARGE")


@pytest.mark.asyncio
async def test_the_413_is_logged_by_the_request_logger(api: Api, caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.INFO, logger="app.request"):
        await api.post("/tokens", "owner", content=b"{}", headers={"Content-Length": str(10**9)})

    assert any("413" in record.getMessage() for record in caplog.records)


def multipart_prefix(filename: str = "p.zip") -> bytes:
    return (
        f"--{BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/zip\r\n\r\n"
    ).encode()


MULTIPART_SUFFIX = f"\r\n--{BOUNDARY}--\r\n".encode()


@pytest.fixture
def small_packages(monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(settings.registry.limits, "max_package_bytes", 3000)
    return 3000


@pytest.mark.asyncio
async def test_an_upload_far_over_the_limit_is_a_413_by_declared_length(
    api: Api, small_packages: int, blob_store
):
    data = b"PK" + b"\x00" * (small_packages + MULTIPART_ALLOWANCE + 1000)

    response = await api.upload(data, "owner")

    assert_error(response, 413, "PAYLOAD_TOO_LARGE")
    assert (await api.get("/skills", "admin")).json()["total"] == 0
    assert list(blob_store.root.rglob("*.zip")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["anonymous", "owner_read", "owner"])
async def test_the_413_comes_before_authorization_for_oversized_uploads(
    api: Api, small_packages: int, actor: str
):
    data = b"\x00" * (small_packages + MULTIPART_ALLOWANCE + 1)

    response = await api.upload(data, actor)

    assert_error(response, 413, "PAYLOAD_TOO_LARGE")


@pytest.mark.asyncio
async def test_a_chunked_multipart_upload_over_the_limit_is_cut_off_without_reading_the_rest(
    api: Api, small_packages: int, blob_store
):
    consumed: list[int] = []
    chunk = b"\x00" * 16 * 1024

    async def body() -> AsyncIterator[bytes]:
        yield multipart_prefix()
        async for piece in endless(chunk, 200, consumed):
            yield piece
        yield MULTIPART_SUFFIX

    response = await api.post("/packages", "owner", content=body(), headers=MULTIPART)

    assert_error(response, 413, "PAYLOAD_TOO_LARGE")
    limit = small_packages + MULTIPART_ALLOWANCE
    assert len(consumed) * len(chunk) <= limit + 2 * len(chunk)
    assert len(consumed) < 200
    assert list(blob_store.root.rglob("*.zip")) == []


@pytest.mark.asyncio
async def test_a_multipart_upload_that_lies_about_its_length_is_still_cut_off(
    api: Api, small_packages: int
):
    payload = multipart_prefix() + b"\x00" * (small_packages + MULTIPART_ALLOWANCE + 500)
    payload += MULTIPART_SUFFIX

    response = await api.post(
        "/packages",
        "owner",
        content=payload,
        headers={**MULTIPART, "Content-Length": "200"},
    )

    assert_error(response, 413, "PAYLOAD_TOO_LARGE")


@pytest.mark.asyncio
async def test_a_package_exactly_at_the_limit_is_accepted(
    api: Api, monkeypatch: pytest.MonkeyPatch
):
    data = make_skill_zip("edge-skill", "1.0.0")
    monkeypatch.setattr(settings.registry.limits, "max_package_bytes", len(data))

    response = await api.upload(data, "owner")

    assert response.status_code == 201
    assert response.json()["version"]["size_bytes"] == len(data)


@pytest.mark.asyncio
async def test_a_package_one_byte_over_the_limit_is_a_413_and_is_never_inspected(
    api: Api, monkeypatch: pytest.MonkeyPatch, blob_store
):
    data = make_skill_zip("edge-skill", "1.0.0")
    monkeypatch.setattr(settings.registry.limits, "max_package_bytes", len(data) - 1)
    inspected: list[int] = []
    original = package_service_module.inspect_package

    def spy(*args: Any, **kwargs: Any):
        inspected.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(package_service_module, "inspect_package", spy)

    response = await api.upload(data, "owner")

    error = assert_error(response, 413, "PAYLOAD_TOO_LARGE")
    assert str(len(data) - 1) in error["message"]
    assert inspected == []
    assert (await api.get("/skills", "admin")).json()["total"] == 0
    assert list(blob_store.root.rglob("*.zip")) == []


@pytest.mark.asyncio
async def test_a_package_over_the_limit_but_inside_the_multipart_allowance_is_a_413_from_the_route(
    api: Api, small_packages: int
):
    data = b"PK" + b"\x00" * (small_packages + 5000)
    assert len(data) < small_packages + MULTIPART_ALLOWANCE

    response = await api.upload(data, "owner")

    assert_error(response, 413, "PAYLOAD_TOO_LARGE")


@pytest.mark.asyncio
async def test_the_upload_route_reads_no_more_than_one_byte_past_the_limit(
    small_packages: int,
):
    with tempfile.SpooledTemporaryFile() as spooled:
        spooled.write(b"z" * 1_000_000)
        spooled.seek(0)
        part = UploadFile(file=spooled, filename="big.zip", size=1_000_000)

        data = await read_at_most(part, small_packages + 1)

        assert len(data) == small_packages + 1
        assert spooled.tell() == small_packages + 1


@pytest.mark.asyncio
async def test_the_package_limit_does_not_loosen_the_limit_of_other_routes(
    api: Api, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings.registry.limits, "max_package_bytes", 10 * ONE_MIB)

    response = await api.post(
        "/tokens",
        "owner",
        content=json_padded_to(2 * ONE_MIB),
        headers={"Content-Type": "application/json"},
    )

    assert_error(response, 413, "PAYLOAD_TOO_LARGE")


@pytest.mark.asyncio
async def test_a_413_never_leaks_internals(api: Api, small_packages: int):
    response = await api.upload(b"\x00" * (small_packages + MULTIPART_ALLOWANCE + 1), "owner")

    assert_no_leak(response)
    assert body_limit.PAYLOAD_TOO_LARGE_CODE in response.text
