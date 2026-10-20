from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.requests import ClientDisconnect, Request

from app.api import errors as errors_module
from app.api.errors import (
    DOMAIN_ERRORS,
    describe_validation_errors,
    error_body,
    error_response,
    handle_client_disconnect,
    handle_domain_error,
    handle_http_exception,
    handle_storage_error,
    handle_unhandled_error,
    handle_validation_error,
    resolve_domain_error,
)
from app.main import app
from app.services.errors import (
    ConflictError,
    DomainError,
    ForbiddenError,
    InvalidPackageError,
    NotFoundError,
    PayloadTooLargeError,
    StorageError,
    UnauthorizedError,
    ValidationError,
)
from app.services.semver import InvalidVersionError
from app.services.skill_service import SkillService
from tests.test_api.support import PUBLISHED_ENTRY, Api, Catalog, assert_error, assert_no_leak

CONTRACT = [
    (NotFoundError, 404, "NOT_FOUND"),
    (ConflictError, 409, "CONFLICT"),
    (ValidationError, 422, "VALIDATION_ERROR"),
    (InvalidPackageError, 422, "INVALID_PACKAGE"),
    (ForbiddenError, 403, "FORBIDDEN"),
    (UnauthorizedError, 401, "UNAUTHORIZED"),
    (PayloadTooLargeError, 413, "PAYLOAD_TOO_LARGE"),
    (StorageError, 500, "STORAGE_ERROR"),
]


def make_error(cls: type[DomainError], message: str = "something specific") -> DomainError:
    if cls is InvalidPackageError:
        return InvalidPackageError("NOT_A_ZIP", message)
    return cls(message)


def fake_request(method: str = "GET", path: str = "/api/v1/probe") -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(b"host", b"test")],
            "server": ("test", 80),
            "scheme": "http",
        }
    )


def raises(error: BaseException):
    async def fail(*args: Any, **kwargs: Any):
        raise error

    return fail


@pytest.mark.parametrize(("cls", "status", "code"), CONTRACT)
def test_the_translation_table_is_the_contract(cls, status, code):
    assert DOMAIN_ERRORS[cls] == (status, code)
    assert cls.code == code
    assert resolve_domain_error(make_error(cls)) == (status, code)


def test_the_table_covers_every_domain_error_the_services_define():
    assert set(DOMAIN_ERRORS) == {cls for cls, _, _ in CONTRACT}


def test_subclasses_inherit_the_translation_of_their_parent():
    assert resolve_domain_error(InvalidVersionError("bad")) == (422, "VALIDATION_ERROR")


def test_an_unmapped_domain_error_has_no_translation():
    assert resolve_domain_error(DomainError("abstract")) is None


def test_the_error_body_omits_a_missing_reason():
    assert error_body("X", "message") == {"error": {"code": "X", "message": "message"}}
    assert error_body("X", "message", "WHY") == {
        "error": {"code": "X", "message": "message", "reason": "WHY"}
    }


def test_error_responses_carry_the_challenge_on_401_only():
    unauthorized = error_response(401, "UNAUTHORIZED", "no")
    forbidden = error_response(403, "FORBIDDEN", "no")

    assert unauthorized.headers["www-authenticate"] == "Bearer"
    assert "www-authenticate" not in forbidden.headers
    assert unauthorized.headers["content-type"] == "application/json"


def test_bodyless_statuses_have_no_body():
    response = error_response(304, "X", "m", headers={"ETag": '"x"'})

    assert response.body == b""
    assert response.headers["etag"] == '"x"'


@pytest.mark.parametrize(("cls", "status", "code"), CONTRACT)
@pytest.mark.asyncio
async def test_every_domain_error_reaches_the_client_in_the_documented_format(
    api: Api, catalog: Catalog, monkeypatch: pytest.MonkeyPatch, cls, status, code
):
    monkeypatch.setattr(SkillService, "get", raises(make_error(cls)))

    response = await api.get(f"/skills/{PUBLISHED_ENTRY}")

    error = assert_error(response, status, code)
    if cls is StorageError:
        assert error["message"] == "The stored package data could not be served"
        assert "reason" not in error
    elif cls is InvalidPackageError:
        assert error["reason"] == "NOT_A_ZIP"
        assert error["message"] == "something specific"
    else:
        assert error["message"] == "something specific"
        assert "reason" not in error


@pytest.mark.asyncio
async def test_a_validation_error_may_carry_a_reason(
    api: Api, catalog: Catalog, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        SkillService, "get", raises(ValidationError("bad path", reason="INVALID_PATH"))
    )

    response = await api.get(f"/skills/{PUBLISHED_ENTRY}")

    assert_error(response, 422, "VALIDATION_ERROR", reason="INVALID_PATH")


@pytest.mark.asyncio
async def test_a_storage_error_is_logged_with_detail_but_answers_without_it(
    api: Api, catalog: Catalog, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    secret = "/srv/alexandria/blobs/ab/abcdef.zip is corrupt"
    monkeypatch.setattr(SkillService, "get", raises(StorageError(secret)))

    with caplog.at_level(logging.ERROR, logger="app.api.errors"):
        response = await api.get(f"/skills/{PUBLISHED_ENTRY}")

    assert_error(response, 500, "STORAGE_ERROR")
    assert "/srv/alexandria" not in response.text
    assert "abcdef" not in response.text
    records = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert any(secret in record.getMessage() for record in records)
    assert all(record.exc_info for record in records)


@pytest.mark.asyncio
async def test_a_domain_error_nobody_mapped_is_an_internal_error_not_a_guess(
    lenient_api: Api, catalog: Catalog, monkeypatch: pytest.MonkeyPatch, caplog
):
    monkeypatch.setattr(SkillService, "get", raises(DomainError("abstract failure")))

    with caplog.at_level(logging.ERROR, logger="app.api.errors"):
        response = await lenient_api.get(f"/skills/{PUBLISHED_ENTRY}")

    assert_error(response, 500, "INTERNAL_ERROR")
    assert "abstract failure" not in response.text
    assert any("Unmapped DomainError" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        ValueError("a plain bug at /home/dev/app/services/x.py line 3"),
        RuntimeError("boom with secret-token-123"),
        KeyError("missing"),
        ZeroDivisionError("division by zero"),
        AssertionError("invariant"),
    ],
)
async def test_an_unexpected_exception_is_a_generic_500_that_hides_the_bug(
    lenient_api: Api,
    catalog: Catalog,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
):
    monkeypatch.setattr(SkillService, "get", raises(error))

    with caplog.at_level(logging.ERROR, logger="app.api.errors"):
        response = await lenient_api.get(f"/skills/{PUBLISHED_ENTRY}")

    body = assert_error(response, 500, "INTERNAL_ERROR")
    assert body["message"] == "An internal error occurred"
    assert "secret-token" not in response.text
    assert "/home/dev" not in response.text
    logged = [record for record in caplog.records if record.name == "app.api.errors"]
    assert logged and logged[0].levelno == logging.ERROR
    assert logged[0].exc_info is not None


@pytest.mark.asyncio
async def test_a_plain_value_error_is_no_longer_reported_as_a_validation_problem(
    lenient_api: Api, catalog: Catalog, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(SkillService, "get", raises(ValueError("looks like bad input")))

    response = await lenient_api.get(f"/skills/{PUBLISHED_ENTRY}")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"


@pytest.mark.asyncio
async def test_the_test_client_sees_the_original_exception_when_it_asks_for_it(
    api: Api, catalog: Catalog, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(SkillService, "get", raises(RuntimeError("visible to the harness")))

    with pytest.raises(RuntimeError, match="visible to the harness"):
        await api.get(f"/skills/{PUBLISHED_ENTRY}")


@pytest.mark.asyncio
async def test_an_unknown_route_is_a_formatted_404(api: Api):
    for path in ("/nothing", "/api/v1/nothing", "/api/v1/skills/a/b/c/d/e/f"):
        response = await api.get(path)
        assert_error(response, 404, "NOT_FOUND")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "allowed"),
    [
        ("DELETE", "/health", {"GET"}),
        ("POST", "/skills", {"GET"}),
        ("PUT", f"/skills/{PUBLISHED_ENTRY}", {"GET", "DELETE"}),
        ("PATCH", "/tokens", {"GET", "POST"}),
        ("GET", "/packages", {"POST"}),
        ("POST", f"/skills/{PUBLISHED_ENTRY}/versions", {"GET"}),
    ],
)
async def test_a_wrong_method_is_a_formatted_405_that_keeps_the_allow_header(
    api: Api, catalog: Catalog, method: str, path: str, allowed: set[str]
):
    response = await api.request(method, path, "owner")

    assert_error(response, 405, "METHOD_NOT_ALLOWED")
    advertised = {item.strip() for item in response.headers["allow"].split(",")}
    assert advertised and advertised <= allowed


@pytest.mark.asyncio
async def test_validation_errors_name_every_offending_field(api: Api, catalog: Catalog):
    response = await api.get("/skills", params={"limit": "abc", "offset": -1})

    error = assert_error(response, 422, "VALIDATION_ERROR")
    assert error["message"].startswith("Invalid request: ")
    assert "query.limit" in error["message"]
    assert "query.offset" in error["message"]


@pytest.mark.asyncio
async def test_validation_errors_never_echo_the_offending_value(api: Api):
    response = await api.post(
        "/auth/login", json={"username": "u", "password": "p" * 2000 + "SECRET-MARKER"}
    )

    assert_error(response, 422, "VALIDATION_ERROR")
    assert "SECRET-MARKER" not in response.text


def test_validation_messages_are_capped_and_summarised():
    errors = [{"loc": ("body", f"field{i}"), "msg": "Field required"} for i in range(25)]

    message = describe_validation_errors(errors)

    assert message.count("Field required") == 10
    assert message.endswith("and 15 more")
    assert "field0" in message and "field24" not in message


def test_validation_messages_have_a_default_for_odd_errors():
    assert describe_validation_errors([{}]) == "Invalid request: request: Invalid value"
    assert "x" * 201 not in describe_validation_errors([{"loc": ("q",), "msg": "x" * 500}])


@pytest.mark.asyncio
async def test_the_validation_handler_answers_422_with_the_shared_format():
    exc = RequestValidationError([{"loc": ("query", "limit"), "msg": "bad", "type": "x"}])

    response = await handle_validation_error(fake_request(), exc)

    assert response.status_code == 422
    assert response.body == (
        b'{"error":{"code":"VALIDATION_ERROR","message":"Invalid request: query.limit: bad"}}'
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, "BAD_REQUEST"),
        (401, "UNAUTHORIZED"),
        (403, "FORBIDDEN"),
        (404, "NOT_FOUND"),
        (405, "METHOD_NOT_ALLOWED"),
        (409, "CONFLICT"),
        (413, "PAYLOAD_TOO_LARGE"),
        (415, "UNSUPPORTED_MEDIA_TYPE"),
        (422, "VALIDATION_ERROR"),
        (429, "TOO_MANY_REQUESTS"),
        (418, "HTTP_418"),
    ],
)
async def test_http_exceptions_use_the_same_format_with_a_stable_code(status: int, code: str):
    exc = HTTPException(status_code=status, detail="visible detail", headers={"X-Extra": "kept"})

    response = await handle_http_exception(fake_request(), exc)

    assert response.status_code == status
    assert response.headers["x-extra"] == "kept"
    assert response.body == error_response(status, code, "visible detail").body
    if status == 401:
        assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.asyncio
async def test_an_http_exception_without_detail_falls_back_to_the_standard_phrase():
    response = await handle_http_exception(fake_request(), HTTPException(status_code=404))

    assert b'"message":"Not Found"' in response.body


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [204, 304])
async def test_http_exceptions_for_bodyless_statuses_send_no_body(status: int):
    response = await handle_http_exception(fake_request(), HTTPException(status_code=status))

    assert response.status_code == status
    assert response.body == b""


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [500, 502, 503])
async def test_server_side_http_exceptions_hide_their_detail_and_are_logged(
    status: int, caplog: pytest.LogCaptureFixture
):
    exc = HTTPException(status_code=status, detail="database password is hunter2")

    with caplog.at_level(logging.ERROR, logger="app.api.errors"):
        response = await handle_http_exception(fake_request(), exc)

    assert b"hunter2" not in response.body
    assert any("hunter2" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_the_domain_handler_serves_domain_errors_directly():
    response = await handle_domain_error(fake_request(), ConflictError("taken"))

    assert response.status_code == 409
    assert response.body == b'{"error":{"code":"CONFLICT","message":"taken"}}'


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", [handle_domain_error, handle_storage_error])
async def test_storage_errors_get_the_generic_answer_from_either_handler(handler):
    response = await handler(fake_request(), StorageError("/srv/blobs/ab/abc.zip is unreadable"))

    assert response.status_code == 500
    assert b"/srv/blobs" not in response.body
    assert b'"code":"STORAGE_ERROR"' in response.body


def test_the_handlers_are_registered_and_no_value_error_handler_remains():
    registered = set(app.exception_handlers)

    assert {
        DomainError,
        StorageError,
        RequestValidationError,
        HTTPException,
        Exception,
    } <= registered
    assert ValueError not in registered


@pytest.mark.asyncio
async def test_a_client_that_hangs_up_is_a_quiet_event_not_a_server_error(
    caplog: pytest.LogCaptureFixture,
):
    with caplog.at_level(logging.DEBUG, logger="app.api.errors"):
        response = await handle_client_disconnect(fake_request("POST"), ClientDisconnect())

    assert response.status_code == 400
    assert errors_module.CLIENT_DISCONNECT_MESSAGE.encode() in response.body
    assert [record.levelno for record in caplog.records] == [logging.INFO]


@pytest.mark.asyncio
async def test_an_upload_abandoned_midway_ends_quietly_with_a_400(
    api: Api, tokens, caplog: pytest.LogCaptureFixture
):
    sent: list[dict[str, Any]] = []
    script = [
        {
            "type": "http.request",
            "body": b"--zz\r\nContent-Disposition: form-da",
            "more_body": True,
        },
        {"type": "http.disconnect"},
    ]

    async def receive():
        return script.pop(0) if script else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "path": "/api/v1/packages",
        "raw_path": b"/api/v1/packages",
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "server": ("test", 80),
        "client": ("127.0.0.1", 1234),
        "headers": [
            (b"authorization", f"Bearer {tokens['owner']}".encode()),
            (b"content-type", b"multipart/form-data; boundary=zz"),
        ],
    }

    with caplog.at_level(logging.INFO):
        await app(scope, receive, send)

    start = next(message for message in sent if message["type"] == "http.response.start")
    assert start["status"] == 400
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


@pytest.mark.asyncio
async def test_the_catch_all_handler_is_generic(caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.ERROR, logger="app.api.errors"):
        response = await handle_unhandled_error(fake_request(), RuntimeError("hunter2"))

    assert response.status_code == 500
    assert b"hunter2" not in response.body
    assert errors_module.INTERNAL_ERROR_MESSAGE.encode() in response.body


HOSTILE_REQUESTS = [
    ("GET", "/skills/%00"),
    ("GET", "/skills/%ff%fe"),
    ("GET", "/skills/" + "a" * 5000),
    ("GET", "/skills/ünï/versions/%00"),
    ("GET", f"/skills/{PUBLISHED_ENTRY}/versions/" + "9" * 400),
    ("GET", f"/skills/{PUBLISHED_ENTRY}/versions/latest/files/%00"),
    ("GET", f"/skills/{PUBLISHED_ENTRY}/versions/latest/raw/..%2F..%2F..%2Fetc%2Fpasswd"),
    ("GET", f"/skills/{PUBLISHED_ENTRY}/versions/latest/files/" + "x/" * 400),
    ("GET", "/skills?q=%00"),
    ("GET", "/skills?q=%27%20OR%201%3D1%20--"),
    ("GET", "/skills?q=%25%25%25%5F%5F%5C"),
    ("GET", "/skills?tag=%00"),
    ("GET", "/skills?category=" + "c" * 4000),
    ("GET", "/skills?limit=999999999999999999999"),
    ("GET", "/skills?offset=99999999999999999999999"),
    ("GET", "/skills?sort=%27%3B%20DROP%20TABLE%20skills%3B%20--"),
    ("GET", "/tags?kind=%00"),
    ("GET", "/categories?limit=1e3"),
    ("DELETE", "/tokens/%00"),
    ("DELETE", "/tokens/" + "z" * 4000),
    ("POST", f"/skills/{PUBLISHED_ENTRY}/versions/%00/publish"),
    ("POST", "/skills/%00/restore"),
    ("DELETE", "/skills/%00"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path"), HOSTILE_REQUESTS)
@pytest.mark.parametrize("actor", ["anonymous", "owner", "admin"])
async def test_hostile_inputs_never_produce_a_server_error_or_a_leak(
    lenient_api: Api, catalog: Catalog, method: str, path: str, actor: str
):
    response = await lenient_api.request(method, path, actor)

    assert response.status_code < 500, response.text
    if response.status_code >= 400:
        assert response.headers["content-type"].startswith("application/json")
        assert set(response.json()) == {"error"}
        assert_no_leak(response)


@pytest.mark.asyncio
async def test_an_error_response_never_carries_a_stack_trace_or_the_blob_location(
    api: Api, catalog: Catalog, blob_store
):
    probes = [
        await api.get("/skills/missing"),
        await api.get(f"/skills/{PUBLISHED_ENTRY}/versions/9.9.9"),
        await api.get(f"/skills/{PUBLISHED_ENTRY}/versions/1.0.0/files/nope"),
        await api.get(f"/skills/{PUBLISHED_ENTRY}/versions/1.0.0/files/..%2Fx"),
        await api.upload(b"not a zip", "owner"),
        await api.upload(b"", "owner"),
        await api.get("/users"),
        await api.post("/tokens", "owner_read", json={"name": "x"}),
        await api.get("/skills", params={"limit": "x"}),
        await api.get("/nothing"),
    ]

    for response in probes:
        assert response.status_code >= 400
        assert_no_leak(response, str(blob_store.root))
