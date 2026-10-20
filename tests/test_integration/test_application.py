from __future__ import annotations

import importlib.metadata
import inspect
import json
import logging
from collections.abc import Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
import uvicorn

from app import main
from app.config.settings import settings
from app.logging import MCP_TRANSPORT_LOGGER, setup_logging
from app.mcp import context
from tests.test_integration.support import ACCEPT

TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
HEADERS = {"accept": ACCEPT}
TOOL_COUNT = 11


def leaves(error: BaseException) -> list[BaseException]:
    if isinstance(error, BaseExceptionGroup):
        return [leaf for inner in error.exceptions for leaf in leaves(inner)]
    return [error]


def route_table() -> list[tuple[str, str]]:
    return [(type(route).__name__, getattr(route, "path", "")) for route in main.app.routes]


class TestTheEndpointIsMounted:
    def test_both_spellings_of_the_path_are_routed_to_the_gateway(self):
        table = route_table()

        assert ("Mount", "/mcp") in table
        assert ("Route", "/mcp") in table

    @pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
    async def test_a_post_reaches_the_server_without_any_redirect(self, client, mcp_server, path):
        response = await client.post(path, json=TOOLS_LIST, headers=HEADERS)

        assert response.status_code == 200
        assert response.history == []
        assert len(response.json()["result"]["tools"]) == TOOL_COUNT

    @pytest.mark.parametrize("method", ["GET", "DELETE", "PUT", "PATCH"])
    @pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
    async def test_only_post_is_accepted(self, client, mcp_server, method, path):
        response = await client.request(method, path, headers=HEADERS)

        assert response.status_code == 405
        assert response.headers["allow"] == "POST"
        assert response.json()["error"]["code"] == "METHOD_NOT_ALLOWED"

    @pytest.mark.parametrize("path", ["/mcpx", "/mcp/extra", "/MCP", "/mcp/extra/more"])
    async def test_other_paths_do_not_reach_the_server(self, client, mcp_server, path):
        response = await client.post(path, json=TOOLS_LIST, headers=HEADERS)

        assert response.status_code == 404

    async def test_the_endpoint_is_not_part_of_the_rest_api_document(self, client):
        spec = (await client.get("/openapi.json")).json()

        assert not [path for path in spec["paths"] if path.startswith("/mcp")]

    async def test_the_rest_api_and_the_public_site_work_next_to_it(self, client, mcp_server):
        health = await client.get("/api/v1/health")
        landing = await client.get("/")
        registry = await client.get("/registry")
        unknown = await client.get("/registry/nothing/here")

        assert health.json() == {"status": "ok"}
        assert landing.status_code == registry.status_code == 200
        assert landing.headers["content-type"].startswith("text/html")
        assert unknown.status_code == 404
        assert unknown.headers["content-type"].startswith("text/html")

    async def test_the_info_endpoint_advertises_the_mcp_path(self, client):
        info = (await client.get("/api/v1/info")).json()

        assert info["mcp_path"] == "/mcp"

    @pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
    async def test_a_browser_preflight_for_the_endpoint_is_answered_by_the_cors_layer(
        self, client, path
    ):
        response = await client.options(
            path,
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization, content-type, mcp-protocol-version",
            },
        )

        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
        assert "POST" in response.headers["access-control-allow-methods"]
        allowed = response.headers["access-control-allow-headers"].lower()
        assert "authorization" in allowed and "mcp-protocol-version" in allowed

    async def test_responses_of_the_endpoint_carry_the_cors_headers_for_an_allowed_origin(
        self, client, mcp_server
    ):
        response = await client.post(
            "/mcp", json=TOOLS_LIST, headers={**HEADERS, "Origin": "http://localhost:5173"}
        )

        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "http://localhost:5173"

    async def test_the_endpoint_answers_503_until_the_lifespan_has_started_it(self, client):
        response = await client.post("/mcp", json=TOOLS_LIST, headers=HEADERS)

        assert response.status_code == 503
        assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"


class TestTheTestRuntime:
    async def test_the_client_fixture_points_the_mcp_runtime_at_the_test_database(
        self, client, session_factory, blob_store
    ):
        assert context.get_session_factory() is session_factory
        assert context.get_blob_store() is blob_store

    async def test_a_tool_reads_the_database_of_the_test_and_not_the_default_one(
        self, client, mcp_connect, world
    ):
        async with mcp_connect(world.credentials["owner"]) as session:
            result = await session.call_tool("get_skill", {"name": "pub-skill"})

        assert result.is_error is False


class TestTheConnectedClientFixture:
    async def test_it_is_a_working_sdk_client_for_the_mounted_endpoint(self, mcp_client):
        tools = await mcp_client.list_tools()
        found = await mcp_client.call_tool("search_skills", {})

        assert len(tools.tools) == TOOL_COUNT
        assert found.is_error is False
        assert json.loads(found.content[0].text) == {
            "items": [],
            "total": 0,
            "limit": 10,
            "offset": 0,
        }

    async def test_it_identifies_the_server(self, mcp_client):
        assert mcp_client.server_info.name == "alexandria"
        assert mcp_client.server_info.version == importlib.metadata.version("alexandria")


class TestTheVersion:
    def test_the_application_reports_the_installed_distribution_version(self):
        assert main.app.version == importlib.metadata.version("alexandria")
        assert main.application_version() == importlib.metadata.version("alexandria")

    async def test_the_openapi_document_and_the_info_endpoint_agree(self, client):
        spec = (await client.get("/openapi.json")).json()
        info = (await client.get("/api/v1/info")).json()

        assert spec["info"]["version"] == info["version"] == main.app.version

    def test_without_an_installed_distribution_it_falls_back_to_the_first_version(
        self, monkeypatch
    ):
        def missing(name: str) -> str:
            raise importlib.metadata.PackageNotFoundError(name)

        monkeypatch.setattr(main.importlib.metadata, "version", missing)

        assert main.application_version() == main.FALLBACK_VERSION == "0.1.0"


class TestTheEntryPoint:
    def test_it_serves_the_application_with_a_bounded_graceful_shutdown(self, monkeypatch):
        accepted = inspect.signature(uvicorn.run)
        calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

        def recording(*args: Any, **kwargs: Any) -> None:
            accepted.bind(*args, **kwargs)
            calls.append((args, kwargs))

        monkeypatch.setattr(uvicorn, "run", recording)

        main.run()

        assert calls == [
            (
                ("app.main:app",),
                {
                    "host": settings.server.host,
                    "port": settings.server.port,
                    "reload": settings.server.reload,
                    "timeout_graceful_shutdown": main.GRACEFUL_SHUTDOWN_SECONDS,
                },
            )
        ]

    def test_the_shutdown_bound_is_a_positive_whole_number_of_seconds(self):
        assert isinstance(main.GRACEFUL_SHUTDOWN_SECONDS, int)
        assert main.GRACEFUL_SHUTDOWN_SECONDS > 0


@pytest.fixture
def stubbed_infrastructure(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    events: list[str] = []

    async def init_db() -> None:
        events.append("init_db")

    async def close_db() -> None:
        events.append("close_db")

    def setup(level: str) -> None:
        events.append("setup_logging")

    monkeypatch.setattr(main, "init_db", init_db)
    monkeypatch.setattr(main, "close_db", close_db)
    monkeypatch.setattr(main, "setup_logging", setup)
    return events


class TestTheLifespan:
    async def test_mcp_starts_after_the_database_and_stops_before_it_is_closed(
        self, monkeypatch, stubbed_infrastructure
    ):
        events = stubbed_infrastructure

        @asynccontextmanager
        async def recording_mcp():
            events.append("mcp:start")
            try:
                yield
            finally:
                events.append("mcp:stop")

        monkeypatch.setattr(main, "mcp_lifespan", recording_mcp)

        async with main.lifespan(main.app):
            events.append("serving")

        assert events == [
            "setup_logging",
            "init_db",
            "mcp:start",
            "serving",
            "mcp:stop",
            "close_db",
        ]

    async def test_the_endpoint_serves_exactly_while_the_application_runs(
        self, client, stubbed_infrastructure
    ):
        before = await client.post("/mcp", json=TOOLS_LIST, headers=HEADERS)
        async with main.lifespan(main.app):
            during = await client.post("/mcp", json=TOOLS_LIST, headers=HEADERS)
        after = await client.post("/mcp", json=TOOLS_LIST, headers=HEADERS)

        assert (before.status_code, during.status_code, after.status_code) == (503, 200, 503)
        assert stubbed_infrastructure == ["setup_logging", "init_db", "close_db"]

    async def test_a_failure_while_serving_still_stops_mcp(self, client, stubbed_infrastructure):
        with pytest.raises(BaseExceptionGroup) as raised:
            async with main.lifespan(main.app):
                raise RuntimeError("boom")

        after = await client.post("/mcp", json=TOOLS_LIST, headers=HEADERS)
        assert after.status_code == 503
        assert [str(leaf) for leaf in leaves(raised.value)] == ["boom"]

    async def test_the_application_can_start_and_stop_repeatedly(
        self, client, stubbed_infrastructure
    ):
        for _ in range(3):
            async with main.lifespan(main.app):
                response = await client.post("/mcp", json=TOOLS_LIST, headers=HEADERS)
                assert response.status_code == 200

        assert stubbed_infrastructure.count("init_db") == 3
        assert stubbed_infrastructure.count("close_db") == 3


@pytest.fixture
def isolated_logging() -> Iterator[logging.Logger]:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    transport = logging.getLogger(MCP_TRANSPORT_LOGGER)
    transport_level = transport.level
    yield transport
    root.handlers[:] = handlers
    root.setLevel(level)
    transport.setLevel(transport_level)


class Collector(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


class TestTheSdkNoiseIsSilenced:
    @pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING"])
    def test_the_transport_logger_stays_at_warning(self, isolated_logging, level):
        setup_logging(level)

        assert isolated_logging.level == logging.WARNING
        assert not isolated_logging.isEnabledFor(logging.INFO)
        assert isolated_logging.isEnabledFor(logging.WARNING)

    @pytest.mark.parametrize(("level", "expected"), [("ERROR", logging.ERROR), ("CRITICAL", 50)])
    def test_a_stricter_threshold_still_applies_to_it(self, isolated_logging, level, expected):
        setup_logging(level)

        assert isolated_logging.level == expected

    def test_the_per_request_session_message_is_dropped_but_warnings_are_kept(
        self, isolated_logging
    ):
        setup_logging("INFO")
        collector = Collector()
        logging.getLogger().addHandler(collector)

        isolated_logging.info("Terminating session: None")
        isolated_logging.warning("something is off")
        logging.getLogger("app.request").info("GET /api/v1/health -> 200")

        assert collector.messages == ["something is off", "GET /api/v1/health -> 200"]

    def test_the_other_loggers_keep_following_the_configured_level(self, isolated_logging):
        setup_logging("INFO")
        collector = Collector()
        logging.getLogger().addHandler(collector)

        logging.getLogger("app.services.package").info("Package uploaded")
        logging.getLogger("app.services.package").debug("not shown at INFO")

        assert collector.messages == ["Package uploaded"]

    def test_an_unknown_level_name_falls_back_to_info(self, isolated_logging):
        setup_logging("NOT-A-LEVEL")

        assert logging.getLogger().level == logging.INFO
        assert isolated_logging.level == logging.WARNING
