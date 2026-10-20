from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import DEFAULT_MAX_REQUEST_BODY_SIZE
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.mcp as mcp_package
from app.api import body_limit as rest_body_limit
from app.config.settings import settings
from app.db.session import configure_sqlite_engine
from app.mcp import body_limit
from app.mcp.asgi import (
    ENVELOPE_ALLOWANCE,
    MCP_PATH,
    McpGateway,
    bearer_credential,
    endpoint_scope,
    mount_mcp,
    route_path,
    upload_body_limit,
)
from app.mcp.body_limit import too_large_message
from app.mcp.context import configure_runtime
from app.models import Base, Skill
from app.services import package_service
from app.services.errors import UnauthorizedError
from app.services.package_service import base64_length
from tests.helpers.factories import make_actors
from tests.helpers.packages import make_skill_zip
from tests.test_mcp.support import (
    DRAFT,
    PDF,
    issue_credentials,
    open_client,
    payload_of,
    post,
    rpc,
    rpc_call,
    seed_registry,
    text_of,
    zip_to_base64,
)

TOOL_COUNT = 11


def tool_count(response: httpx.Response) -> int:
    return len(response.json()["result"]["tools"])


class TestMethodsAndPaths:
    @pytest.mark.parametrize("method", ["GET", "DELETE", "PUT", "PATCH", "OPTIONS"])
    async def test_only_post_is_accepted_and_nothing_is_left_open(self, gateway, method):
        response = await post(gateway, rpc("tools/list"), method=method)

        assert response.status_code == 405
        assert response.headers["allow"] == "POST"
        assert response.json()["error"]["code"] == "METHOD_NOT_ALLOWED"

    async def test_get_does_not_open_an_event_stream(self, gateway):
        response = await post(gateway, None, method="GET", headers={"accept": "text/event-stream"})

        assert response.status_code == 405
        assert response.headers["content-type"] == "application/json"

    async def test_head_requests_are_refused_too(self, gateway):
        response = await post(gateway, None, method="HEAD")

        assert response.status_code == 405

    @pytest.mark.parametrize("path", ["/mcp", "/mcp/", "/"])
    async def test_the_endpoint_answers_on_its_path_with_or_without_the_trailing_slash(
        self, gateway, path
    ):
        response = await post(gateway, rpc("tools/list"), path=path)

        assert response.status_code == 200
        assert tool_count(response) == TOOL_COUNT

    @pytest.mark.parametrize(
        "path",
        ["/mcp/extra", "/other", "/mcp/mcp", "/api/v1/skills", "/mcp%2Fx", "/MCP", "/mcp//"],
    )
    async def test_any_other_path_is_not_found(self, gateway, path):
        response = await post(gateway, rpc("tools/list"), path=path)

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"

    async def test_a_custom_endpoint_path_can_be_configured(self):
        gateway = McpGateway(path="/rpc")
        async with gateway.lifespan():
            served = await post(gateway, rpc("tools/list"), path="/rpc")
            default = await post(gateway, rpc("tools/list"), path="/mcp")

        assert served.status_code == 200
        assert default.status_code == 404


class TestOtherProtocols:
    async def test_websocket_connections_are_closed_politely(self, gateway):
        sent: list[dict] = []

        async def receive():
            return {"type": "websocket.connect"}

        async def send(message):
            sent.append(message)

        await gateway({"type": "websocket", "path": "/mcp", "headers": []}, receive, send)

        assert sent == [{"type": "websocket.close", "code": 1008}]

    async def test_lifespan_events_are_ignored_because_the_host_application_owns_them(
        self, gateway
    ):
        sent: list[dict] = []

        async def receive():
            raise AssertionError("the lifespan channel must not be read")

        async def send(message):
            sent.append(message)

        await gateway({"type": "lifespan"}, receive, send)

        assert sent == []


class TestMalformedRequests:
    async def test_invalid_json_is_a_json_rpc_parse_error(self, gateway):
        response = await post(gateway, content=b"{not json")

        assert response.status_code == 400
        assert response.json()["error"]["code"] == -32700

    @pytest.mark.parametrize(
        "content",
        [b"[]", b'"text"', b"42", b"null", b'{"jsonrpc": "2.0"}', b"[" * 50_000 + b"]" * 50_000],
    )
    async def test_bodies_that_are_not_a_single_request_do_not_crash_the_server(
        self, gateway, content
    ):
        response = await post(gateway, content=content)

        assert response.status_code in {400, 422}

    async def test_a_wrong_content_type_is_refused(self, gateway):
        response = await post(
            gateway,
            content=b"hello",
            headers={"content-type": "text/plain"},
        )

        assert response.status_code in {400, 415}

    async def test_a_client_that_cannot_accept_json_is_refused(self, gateway):
        response = await post(gateway, rpc("tools/list"), headers={"accept": "text/html"})

        assert response.status_code == 406

    async def test_the_server_keeps_working_after_malformed_requests(self, gateway):
        await post(gateway, content=b"{broken")
        await post(gateway, content=b"[]")

        assert tool_count(await post(gateway, rpc("tools/list"))) == TOOL_COUNT

    @pytest.mark.parametrize(
        ("host", "origin"),
        [
            ("registry.example.org", None),
            ("localhost:8000", "http://localhost:5173"),
            ("evil.example", "https://evil.example"),
        ],
    )
    async def test_host_and_origin_filtering_belongs_to_the_application_not_to_this_endpoint(
        self, gateway, host, origin
    ):
        headers = {} if origin is None else {"origin": origin}

        response = await post(
            gateway, rpc("tools/list"), base_url=f"http://{host}", headers=headers
        )

        assert response.status_code == 200


class TestLifecycle:
    async def test_requests_before_startup_get_a_clear_503(self, caplog):
        gateway = McpGateway()

        with caplog.at_level(logging.ERROR, logger="app.mcp.asgi"):
            response = await post(gateway, rpc("tools/list"))

        assert response.status_code == 503
        assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
        assert "not running" in response.json()["error"]["message"]
        assert any("not running" in record.getMessage() for record in caplog.records)

    async def test_the_gateway_serves_only_while_its_lifespan_is_active(self):
        gateway = McpGateway()

        async with gateway.lifespan():
            assert (await post(gateway, rpc("tools/list"))).status_code == 200
        assert (await post(gateway, rpc("tools/list"))).status_code == 503

    async def test_the_lifespan_can_be_entered_again_after_it_ended(self):
        gateway = McpGateway()

        for _ in range(3):
            async with gateway.lifespan():
                assert tool_count(await post(gateway, rpc("tools/list"))) == TOOL_COUNT

        assert (await post(gateway, rpc("tools/list"))).status_code == 503

    async def test_nested_lifespans_keep_the_gateway_running_until_the_last_one_ends(self):
        gateway = McpGateway()

        async with gateway.lifespan():
            async with gateway.lifespan():
                assert (await post(gateway, rpc("tools/list"))).status_code == 200
            assert (await post(gateway, rpc("tools/list"))).status_code == 200
        assert (await post(gateway, rpc("tools/list"))).status_code == 503

    async def test_lifespans_of_different_tasks_that_end_out_of_order_do_not_stop_each_other(
        self,
    ):
        gateway = McpGateway()
        started = [asyncio.Event(), asyncio.Event()]
        finish = [asyncio.Event(), asyncio.Event()]

        async def run(index: int) -> None:
            async with gateway.lifespan():
                started[index].set()
                await finish[index].wait()

        tasks = [asyncio.create_task(run(0)), asyncio.create_task(run(1))]
        await asyncio.gather(*(event.wait() for event in started))

        finish[0].set()
        await tasks[0]
        assert (await post(gateway, rpc("tools/list"))).status_code == 200
        finish[1].set()
        await tasks[1]

        assert (await post(gateway, rpc("tools/list"))).status_code == 503

    async def test_the_lifespan_accepts_the_application_like_a_starlette_lifespan(self):
        gateway = McpGateway()

        async with gateway.lifespan(object()):
            assert (await post(gateway, rpc("tools/list"))).status_code == 200

    async def test_independent_gateways_do_not_share_state(self):
        first, second = McpGateway(), McpGateway()

        async with first.lifespan():
            assert (await post(first, rpc("tools/list"))).status_code == 200
            assert (await post(second, rpc("tools/list"))).status_code == 503

    async def test_each_lifespan_builds_a_fresh_server_with_the_eleven_tools(self):
        built: list[MCPServer] = []

        def factory() -> MCPServer:
            server = mcp_package.asgi.build_server()
            built.append(server)
            return server

        gateway = McpGateway(server_factory=factory)
        for _ in range(2):
            async with gateway.lifespan():
                pass

        assert len(built) == 2 and built[0] is not built[1]
        for server in built:
            assert len(await server.list_tools()) == TOOL_COUNT

    async def test_a_failing_server_factory_leaves_the_gateway_stopped(self):
        def factory() -> MCPServer:
            raise RuntimeError("cannot build")

        gateway = McpGateway(server_factory=factory)

        with pytest.raises(RuntimeError, match="cannot build"):
            async with gateway.lifespan():
                pytest.fail("the lifespan must not start")

        assert (await post(gateway, rpc("tools/list"))).status_code == 503

    async def test_the_package_exposes_a_ready_to_mount_app_and_its_lifespan(self):
        assert isinstance(mcp_package.mcp_app, McpGateway)
        assert mcp_package.MCP_PATH == "/mcp"

        async with mcp_package.mcp_lifespan():
            response = await post(mcp_package.mcp_app, rpc("tools/list"))
        assert tool_count(response) == TOOL_COUNT
        assert (await post(mcp_package.mcp_app, rpc("tools/list"))).status_code == 503

    async def test_concurrent_requests_are_served_independently(self, gateway):
        responses = await asyncio.gather(
            *(post(gateway, rpc("tools/list", request_id=index)) for index in range(25))
        )

        assert [response.json()["id"] for response in responses] == list(range(25))
        assert {tool_count(response) for response in responses} == {TOOL_COUNT}


class TestMounting:
    @pytest.fixture
    def host(self, gateway: McpGateway) -> FastAPI:
        application = FastAPI()
        mount_mcp(application, gateway)

        @application.get("/health")
        async def health() -> dict[str, str]:
            return {"status": "ok"}

        return application

    @pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
    async def test_both_spellings_reach_the_endpoint_without_a_redirect(self, host, path):
        response = await post(host, rpc("tools/list"), path=path)

        assert response.status_code == 200
        assert tool_count(response) == TOOL_COUNT

    @pytest.mark.usefixtures("registry")
    async def test_the_sdk_client_works_through_the_host_application(self, host, credentials):
        async with open_client(host, credentials.owner_read, mode="auto") as client:
            result = await client.call_tool("get_skill", {"name": PDF})

        assert payload_of(result)["name"] == PDF

    @pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
    async def test_other_methods_are_refused_by_the_endpoint_itself(self, host, path):
        response = await post(host, None, method="GET", path=path)

        assert response.status_code == 405
        assert response.headers["allow"] == "POST"

    async def test_the_rest_of_the_host_is_unaffected(self, host):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=host), base_url="http://test"
        ) as client:
            response = await client.get("/health")

        assert response.json() == {"status": "ok"}

    @pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
    async def test_the_helper_defaults_to_the_application_wide_gateway(self, path):
        application = FastAPI()
        mount_mcp(application)

        async with mcp_package.mcp_lifespan():
            response = await post(application, rpc("tools/list"), path=path)

        assert response.status_code == 200
        assert tool_count(response) == TOOL_COUNT

    async def test_the_helper_can_mount_the_endpoint_somewhere_else(self, gateway):
        application = FastAPI()
        other = McpGateway(path="/agents/mcp")
        mount_mcp(application, other, "/agents/mcp")

        async with other.lifespan():
            served = await post(application, rpc("tools/list"), path="/agents/mcp")
            slash = await post(application, rpc("tools/list"), path="/agents/mcp/")
            elsewhere = await post(application, rpc("tools/list"), path="/mcp")

        assert (served.status_code, slash.status_code, elsewhere.status_code) == (200, 200, 404)

    async def test_a_mount_only_host_serves_the_trailing_slash_path(self, gateway):
        application = FastAPI()
        application.mount("/mcp", gateway)

        response = await post(application, rpc("tools/list"), path="/mcp/")

        assert response.status_code == 200

    async def test_a_reverse_proxy_prefix_does_not_confuse_the_path_match(self, gateway):
        transport = httpx.ASGITransport(app=gateway, root_path="/registry")
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/mcp",
                json=rpc("tools/list"),
                headers={"accept": "application/json", "content-type": "application/json"},
            )

        assert response.status_code == 200


class TestPureFunctions:
    @pytest.mark.parametrize(
        ("scope", "expected"),
        [
            ({"path": "/mcp/", "root_path": "/mcp"}, "/"),
            ({"path": "/mcp", "root_path": "/mcp"}, ""),
            ({"path": "/mcp", "root_path": ""}, "/mcp"),
            ({"path": "/mcp"}, "/mcp"),
            ({"path": "/a/b", "root_path": "/a"}, "/b"),
            ({"path": "/b", "root_path": "/a"}, "/b"),
            ({"path": "/"}, "/"),
            ({}, ""),
        ],
    )
    def test_the_route_path_is_the_path_below_the_mount_point(self, scope, expected):
        assert route_path(scope) == expected

    def test_the_endpoint_scope_always_targets_the_root_of_the_inner_application(self):
        scope = {"type": "http", "path": "/mcp/", "raw_path": b"/mcp/", "root_path": "/mcp", "x": 1}

        rewritten = endpoint_scope(scope)

        assert (rewritten["path"], rewritten["raw_path"], rewritten["root_path"]) == ("/", b"/", "")
        assert rewritten["x"] == 1 and rewritten["type"] == "http"
        assert scope["path"] == "/mcp/"

    @pytest.mark.parametrize(
        ("headers", "expected"),
        [
            ([], None),
            ([(b"accept", b"*/*")], None),
            ([(b"authorization", b"Bearer abc")], "abc"),
            ([(b"authorization", b"bearer abc")], "abc"),
            ([(b"authorization", b"Bearer   abc  ")], "abc"),
        ],
    )
    def test_the_bearer_credential_is_extracted_from_the_authorization_header(
        self, headers, expected
    ):
        assert bearer_credential({"headers": headers}) == expected

    @pytest.mark.parametrize(
        "headers",
        [
            [(b"authorization", b"Basic abc")],
            [(b"authorization", b"Bearer")],
            [(b"authorization", b"Bearer a b")],
            [(b"authorization", b"")],
            [(b"authorization", b"Bearer a"), (b"authorization", b"Bearer b")],
        ],
    )
    def test_malformed_headers_raise_unauthorized(self, headers):
        with pytest.raises(UnauthorizedError):
            bearer_credential({"headers": headers})

    def test_the_gateway_hands_every_authorization_value_to_the_shared_parser(self, monkeypatch):
        seen: list[list[str]] = []

        def spy(values):
            seen.append(list(values))
            return "sentinel"

        monkeypatch.setattr(mcp_package.asgi, "extract_bearer", spy)
        scope = {
            "headers": [
                (b"accept", b"*/*"),
                (b"authorization", b"Bearer caf\xe9"),
                (b"x-authorization", b"Bearer other"),
                (b"authorization", b"Basic x"),
            ]
        }

        assert bearer_credential(scope) == "sentinel"
        assert seen == [["Bearer caf\xe9", "Basic x"]]

    def test_the_gateway_no_longer_parses_the_header_itself(self):
        source = Path(mcp_package.asgi.__file__).read_text(encoding="utf-8")

        assert "partition" not in source
        assert "extract_bearer" in source

    def test_the_upload_body_limit_fits_the_largest_package_in_base64(self, monkeypatch):
        limit = settings.registry.limits.max_package_bytes

        assert upload_body_limit() == base64_length(limit) + ENVELOPE_ALLOWANCE
        assert upload_body_limit() > limit * 4 // 3
        monkeypatch.setattr(settings.registry.limits, "max_package_bytes", 1000)
        assert upload_body_limit() == DEFAULT_MAX_REQUEST_BODY_SIZE
        monkeypatch.setattr(settings.registry.limits, "max_package_bytes", 100 * 1024 * 1024)
        assert upload_body_limit() == base64_length(100 * 1024 * 1024) + ENVELOPE_ALLOWANCE

    def test_the_upload_body_limit_is_derived_from_the_single_encoded_length_helper(
        self, monkeypatch
    ):
        monkeypatch.setattr(package_service, "base64_length", lambda size: 9_000_000 + size)
        monkeypatch.setattr(settings.registry.limits, "max_package_bytes", 1000)

        assert upload_body_limit() == 9_001_000 + ENVELOPE_ALLOWANCE

    def test_the_endpoint_path_is_mcp(self):
        assert MCP_PATH == "/mcp"


def assert_json_413(response: httpx.Response, limit: int) -> None:
    assert response.status_code == 413
    assert response.headers["content-type"] == "application/json"
    assert response.headers["connection"] == "close"
    assert "www-authenticate" not in response.headers
    assert response.json() == {
        "error": {"code": "PAYLOAD_TOO_LARGE", "message": too_large_message(limit)}
    }
    assert "Request body too large" not in response.text


class TestRequestBodyLimits:
    async def test_small_requests_pass_for_everyone(self, gateway, credentials):
        for token in (None, credentials.owner_read, credentials.owner_write):
            response = await post(gateway, rpc_call("list_tags"), token=token)
            assert response.status_code == 200

    @pytest.mark.parametrize("name", [None, "owner_read", "stranger_read", "admin_read"])
    async def test_anonymous_and_read_only_callers_cannot_send_large_bodies(
        self, gateway, credentials, name
    ):
        token = None if name is None else getattr(credentials, name)
        oversized = b" " * (DEFAULT_MAX_REQUEST_BODY_SIZE + 1)

        response = await post(gateway, content=oversized, token=token)

        assert_json_413(response, DEFAULT_MAX_REQUEST_BODY_SIZE)

    async def test_the_limit_also_applies_to_streamed_bodies_without_a_length(self, gateway):
        async def chunks() -> AsyncIterator[bytes]:
            for _ in range(5):
                yield b" " * (1024 * 1024)

        response = await post(gateway, content=chunks())

        assert_json_413(response, DEFAULT_MAX_REQUEST_BODY_SIZE)

    async def test_a_body_just_under_the_limit_is_still_parsed(self, gateway):
        padding = " " * (DEFAULT_MAX_REQUEST_BODY_SIZE - 200)
        body = b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' + padding.encode()

        response = await post(gateway, content=body)

        assert response.status_code == 200

    async def test_a_body_exactly_at_the_limit_gets_through_and_one_byte_more_does_not(
        self, gateway
    ):
        at_limit = await post(gateway, content=b" " * DEFAULT_MAX_REQUEST_BODY_SIZE)
        over = await post(gateway, content=b" " * (DEFAULT_MAX_REQUEST_BODY_SIZE + 1))

        assert at_limit.status_code != 413
        assert_json_413(over, DEFAULT_MAX_REQUEST_BODY_SIZE)

    async def test_writers_may_send_packages_larger_than_the_default_body_limit(
        self, gateway, credentials
    ):
        noise = random.Random(11).randbytes(5 * 1024 * 1024)
        data = make_skill_zip("big-upload", "1.0.0", files={"noise.bin": noise})
        assert len(data) > DEFAULT_MAX_REQUEST_BODY_SIZE

        async with open_client(gateway, credentials.owner_write) as client:
            result = await client.call_tool(
                "upload_package", {"content_base64": zip_to_base64(data), "publish": True}
            )

        assert result.is_error is False
        assert payload_of(result)["version"]["size_bytes"] == len(data)

    async def test_readers_get_a_json_413_for_the_same_large_package(self, gateway, credentials):
        noise = random.Random(12).randbytes(5 * 1024 * 1024)
        data = make_skill_zip("big-reader", "1.0.0", files={"noise.bin": noise})
        body = rpc_call("upload_package", {"content_base64": zip_to_base64(data)})

        response = await post(gateway, body, token=credentials.owner_read)

        assert_json_413(response, DEFAULT_MAX_REQUEST_BODY_SIZE)

    async def test_even_writers_cannot_exceed_the_body_limit_derived_from_the_package_limit(
        self, gateway, credentials
    ):
        oversized = b" " * (upload_body_limit() + 1)

        response = await post(gateway, content=oversized, token=credentials.owner_write)

        assert_json_413(response, upload_body_limit())

    async def test_a_streamed_body_over_the_writer_limit_is_a_json_413_too(
        self, gateway, credentials, monkeypatch
    ):
        monkeypatch.setattr(settings.registry.limits, "max_package_bytes", 1000)
        pieces = upload_body_limit() // (1024 * 1024) + 2

        async def chunks() -> AsyncIterator[bytes]:
            for _ in range(pieces):
                yield b" " * (1024 * 1024)

        response = await post(gateway, content=chunks(), token=credentials.owner_write)

        assert_json_413(response, upload_body_limit())

    async def test_the_limit_follows_the_package_limit_of_the_moment(
        self, gateway, credentials, monkeypatch
    ):
        monkeypatch.setattr(settings.registry.limits, "max_package_bytes", 100 * 1024 * 1024)
        body = b" " * (upload_body_limit() - 1)

        accepted = await post(gateway, content=body, token=credentials.owner_write)

        assert accepted.status_code != 413
        assert upload_body_limit() > 100 * 1024 * 1024

    async def test_a_rejected_body_never_reaches_a_tool(
        self, gateway, credentials, session_factory
    ):
        noise = random.Random(14).randbytes(5 * 1024 * 1024)
        data = make_skill_zip("never-stored", "1.0.0", files={"noise.bin": noise})
        body = rpc_call("upload_package", {"content_base64": zip_to_base64(data), "publish": True})

        response = await post(gateway, body, token=credentials.owner_read)

        assert_json_413(response, DEFAULT_MAX_REQUEST_BODY_SIZE)
        async with session_factory() as db:
            names = [skill.name for skill in (await db.execute(select(Skill))).scalars()]
        assert "never-stored" not in names

    async def test_a_package_over_the_registry_limit_gets_a_tool_error_within_the_body_limit(
        self, gateway, credentials, monkeypatch
    ):
        monkeypatch.setattr(settings.registry.limits, "max_package_bytes", 100_000)
        noise = random.Random(13).randbytes(300_000)
        data = make_skill_zip("over-limit", "1.0.0", files={"noise.bin": noise})

        async with open_client(gateway, credentials.owner_write) as client:
            result = await client.call_tool(
                "upload_package", {"content_base64": zip_to_base64(data)}
            )

        assert text_of(result) == "PAYLOAD_TOO_LARGE: The package exceeds the limit of 100000 bytes"

    def test_the_gateway_says_it_the_way_the_rest_api_does(self):
        assert body_limit.PAYLOAD_TOO_LARGE_CODE == rest_body_limit.PAYLOAD_TOO_LARGE_CODE
        assert body_limit.too_large_message(1234) == rest_body_limit.too_large_message(1234)
        assert body_limit.CLOSE_CONNECTION == rest_body_limit.CLOSE_CONNECTION


class TestThroughTheHostApplication:
    async def post_to_the_host(self, client, body: bytes, token: str):
        return await client.post(
            "/mcp",
            content=body,
            headers={
                "authorization": f"Bearer {token}",
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
            },
        )

    async def test_a_reader_over_the_gateway_limit_gets_the_json_413_of_the_gateway(
        self, client, mcp_server, credentials
    ):
        oversized = b" " * (DEFAULT_MAX_REQUEST_BODY_SIZE + 1)

        response = await self.post_to_the_host(client, oversized, credentials.owner_read)

        assert_json_413(response, DEFAULT_MAX_REQUEST_BODY_SIZE)

    async def test_a_writer_over_every_limit_gets_a_413_with_the_same_json_shape(
        self, client, mcp_server, credentials
    ):
        oversized = b" " * (upload_body_limit() + 1)

        response = await self.post_to_the_host(client, oversized, credentials.owner_write)

        assert_json_413(response, upload_body_limit())

    async def test_a_writer_between_the_two_limits_is_not_refused(
        self, client, mcp_server, credentials
    ):
        body = b" " * (DEFAULT_MAX_REQUEST_BODY_SIZE + 1)

        response = await self.post_to_the_host(client, body, credentials.owner_write)

        assert response.status_code != 413


@pytest_asyncio.fixture
async def shared_file_database(tmp_path, blob_store):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'mcp.db'}", connect_args={"timeout": 60}
    )
    configure_sqlite_engine(engine)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    configure_runtime(session_factory=factory, blob_store=blob_store)
    yield factory
    await engine.dispose()


class TestConcurrentIdentities:
    async def test_parallel_requests_from_different_users_never_see_each_others_identity(
        self, gateway, shared_file_database, blob_store
    ):
        async with shared_file_database() as db:
            actors = await make_actors(db)
        await seed_registry(shared_file_database, blob_store, actors)
        credentials = await issue_credentials(shared_file_database, actors)
        identities = {
            "anonymous": (None, False),
            "owner": (credentials.owner_write, True),
            "jwt": (credentials.owner_jwt, True),
            "admin": (credentials.admin_read, True),
            "stranger": (credentials.stranger_write, False),
        }
        for token, _ in identities.values():
            await post(gateway, rpc("tools/list"), token=token)

        async def ask(name: str) -> tuple[str, str]:
            token, _ = identities[name]
            response = await post(gateway, rpc_call("get_skill", {"name": DRAFT}), token=token)
            return name, response.json()["result"]["content"][0]["text"]

        names = list(identities) * 12
        random.Random(5).shuffle(names)
        outcomes = await asyncio.gather(*(ask(name) for name in names))

        for name, text in outcomes:
            visible = identities[name][1]
            assert text.startswith("NOT_FOUND") is not visible, (name, text[:80])
