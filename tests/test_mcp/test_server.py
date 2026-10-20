from __future__ import annotations

import importlib.metadata

import pytest
from mcp import Client, MCPError
from mcp.types import METHOD_NOT_FOUND

from app.mcp import server as server_module
from app.mcp import tools
from app.mcp.server import (
    SERVER_INSTRUCTIONS,
    SERVER_NAME,
    UNKNOWN_VERSION,
    build_server,
    server_version,
)

TOOL_NAMES = [spec.name for spec in tools.TOOLS]


@pytest.fixture(autouse=True)
def fresh_version_cache():
    server_version.cache_clear()
    yield
    server_version.cache_clear()


class TestBuildServer:
    async def test_every_build_registers_the_eleven_tools_in_contract_order(self):
        for _ in range(2):
            listed = await build_server().list_tools()

            assert [tool.name for tool in listed] == [
                "search_skills",
                "get_skill",
                "get_entrypoint",
                "list_files",
                "read_file",
                "get_download_info",
                "list_tags",
                "list_categories",
                "upload_package",
                "publish_version",
                "deprecate_version",
            ]

    def test_servers_are_independent_instances(self):
        assert build_server() is not build_server()

    def test_the_server_describes_itself(self):
        server = build_server()

        assert server.name == SERVER_NAME == "alexandria"
        assert server.title == "Alexandria"
        assert server.description
        assert server.instructions == SERVER_INSTRUCTIONS
        assert server.version == server_version()

    @pytest.mark.parametrize(
        "needle",
        [
            "skill",
            "grounding",
            "SKILL.md",
            "INDEX.md",
            "search_skills",
            "get_entrypoint",
            "list_files",
            "read_file",
            "get_download_info",
            "'latest'",
            "Authorization: Bearer",
            "write scope",
            "CODE: message",
            "NOT_FOUND",
            "STORAGE_ERROR",
            "INTERNAL_ERROR",
        ],
    )
    def test_the_instructions_teach_the_workflow_and_the_error_format(self, needle):
        assert needle in SERVER_INSTRUCTIONS

    def test_the_tools_called_in_the_instructions_exist(self):
        for name in TOOL_NAMES:
            if name in SERVER_INSTRUCTIONS:
                assert getattr(tools, name)


class TestNoChangeStreams:
    @pytest.mark.parametrize("mode", ["legacy", "auto"])
    async def test_no_change_notification_is_announced_in_either_protocol_era(self, mode):
        async with Client(build_server(), mode=mode) as client:
            capabilities = client.server_capabilities

        assert capabilities.tools is not None
        assert capabilities.tools.list_changed is False
        assert capabilities.prompts is not None
        assert capabilities.prompts.list_changed is False
        assert capabilities.resources is not None
        assert capabilities.resources.list_changed is False
        assert capabilities.resources.subscribe is False

    async def test_a_modern_client_cannot_open_a_listen_stream(self):
        async with Client(build_server(), mode="auto") as client:
            assert client.protocol_version == "2026-07-28"
            with pytest.raises(MCPError) as refused:
                async with client.listen(tools_list_changed=True):
                    pytest.fail("the server must not open a listen stream")

        assert refused.value.code == METHOD_NOT_FOUND

    async def test_the_tools_keep_working_after_a_refused_listen(self):
        async with Client(build_server(), mode="auto") as client:
            with pytest.raises(MCPError):
                async with client.listen(tools_list_changed=True):
                    pytest.fail("the server must not open a listen stream")
            listed = await client.list_tools()

        assert [tool.name for tool in listed.tools] == TOOL_NAMES


class TestServerVersion:
    def test_it_reports_the_installed_distribution_version(self):
        assert server_version() == importlib.metadata.version("alexandria")

    def test_it_falls_back_when_the_distribution_is_not_installed(self, monkeypatch):
        def missing(name: str) -> str:
            raise importlib.metadata.PackageNotFoundError(name)

        monkeypatch.setattr(server_module.importlib.metadata, "version", missing)

        assert server_version() == UNKNOWN_VERSION == "0.0.0"

    def test_the_version_is_looked_up_once(self, monkeypatch):
        calls: list[str] = []

        def counting(name: str) -> str:
            calls.append(name)
            return "9.9.9"

        monkeypatch.setattr(server_module.importlib.metadata, "version", counting)

        assert (server_version(), server_version()) == ("9.9.9", "9.9.9")
        assert calls == ["alexandria"]
