from __future__ import annotations

import asyncio
import json
import re

import pytest
from mcp import MCPError
from mcp.types import METHOD_NOT_FOUND

from app.mcp import tools
from app.mcp.server import SERVER_NAME, server_version
from tests.helpers.packages import make_grounding_zip, make_skill_zip
from tests.test_mcp.support import (
    API,
    PDF,
    open_client,
    payload_of,
    post,
    rpc,
    rpc_call,
    text_of,
    zip_to_base64,
)

MODES = ["legacy", "auto", "2026-07-28"]
TOOL_NAMES = [
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
KIND = "kind|null"
REQUIRED = "required"
RESPONSE_BUDGET = 10
MODERN_VERSION = "2026-07-28"
MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": MODERN_VERSION,
    "io.modelcontextprotocol/clientCapabilities": {},
}
MODERN_REQUESTS: list[tuple[str, dict, str | None]] = [
    ("server/discover", {}, None),
    ("tools/list", {}, None),
    ("tools/call", {"name": "list_tags", "arguments": {}}, "list_tags"),
    ("resources/list", {}, None),
    ("resources/templates/list", {}, None),
    ("prompts/list", {}, None),
    ("ping", {}, None),
    ("logging/setLevel", {"level": "info"}, None),
    ("resources/subscribe", {"uri": "file:///none"}, None),
    ("subscriptions/listen", {"notifications": {"toolsListChanged": True}}, None),
]

EXPECTED_PARAMETERS: dict[str, dict[str, tuple[str, object]]] = {
    "search_skills": {
        "query": ("string", ""),
        "kind": (KIND, None),
        "tags": ("array<string>", []),
        "category": ("string|null", None),
        "limit": ("integer", 10),
        "offset": ("integer", 0),
    },
    "get_skill": {"name": ("string", REQUIRED)},
    "get_entrypoint": {"name": ("string", REQUIRED), "version": ("string", "latest")},
    "list_files": {"name": ("string", REQUIRED), "version": ("string", "latest")},
    "read_file": {
        "name": ("string", REQUIRED),
        "path": ("string", REQUIRED),
        "version": ("string", "latest"),
    },
    "get_download_info": {"name": ("string", REQUIRED), "version": ("string", "latest")},
    "list_tags": {"kind": (KIND, None), "limit": ("integer", 50)},
    "list_categories": {"kind": (KIND, None), "limit": ("integer", 50)},
    "upload_package": {
        "content_base64": ("string", REQUIRED),
        "version": ("string|null", None),
        "publish": ("boolean", False),
    },
    "publish_version": {"name": ("string", REQUIRED), "version": ("string", REQUIRED)},
    "deprecate_version": {"name": ("string", REQUIRED), "version": ("string", REQUIRED)},
}


def shape_of(schema: dict) -> str:
    if "anyOf" in schema:
        parts = []
        for option in schema["anyOf"]:
            parts.append("kind" if "enum" in option else option["type"])
        return "|".join(parts)
    if schema["type"] == "array":
        return f"array<{schema['items']['type']}>"
    return schema["type"]


@pytest.fixture(params=MODES)
def mode(request) -> str:
    return request.param


async def listed_tools(app, mode: str):
    async with open_client(app, mode=mode) as client:
        return (await client.list_tools()).tools


class TestHandshake:
    @pytest.mark.parametrize("mode", ["legacy", "auto"])
    async def test_the_server_identifies_itself_and_explains_the_registry(self, gateway, mode):
        async with open_client(gateway, mode=mode) as client:
            assert client.server_info is not None
            assert client.server_info.name == SERVER_NAME == "alexandria"
            assert client.server_info.version == server_version()
            assert client.server_capabilities.tools is not None
            instructions = client.instructions
            assert instructions is not None
            for hint in (
                "search_skills",
                "get_entrypoint",
                "read_file",
                "get_download_info",
                "SKILL.md",
                "INDEX.md",
                "'latest'",
                "CODE: message",
            ):
                assert hint in instructions

    async def test_the_negotiated_protocol_depends_on_the_client(self, gateway):
        async with open_client(gateway, mode="legacy") as legacy:
            assert legacy.protocol_version == "2025-11-25"
        async with open_client(gateway, mode="auto") as modern:
            assert modern.protocol_version == "2026-07-28"

    async def test_no_resources_or_prompts_are_exposed(self, gateway, mode):
        async with open_client(gateway, mode=mode) as client:
            assert (await client.list_resources()).resources == []
            assert (await client.list_resource_templates()).resource_templates == []
            assert (await client.list_prompts()).prompts == []

    async def test_stateless_json_transport_over_raw_http(self, gateway):
        initialize = rpc(
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "raw", "version": "1"},
            },
        )

        first = await post(gateway, initialize)
        listing = await post(gateway, rpc("tools/list", request_id=2))
        call = await post(gateway, rpc_call("list_tags"), headers={"mcp-session-id": "stale"})

        assert first.status_code == 200
        assert first.headers["content-type"].startswith("application/json")
        assert "mcp-session-id" not in first.headers
        assert first.json()["result"]["serverInfo"]["name"] == "alexandria"
        assert listing.status_code == 200
        assert [tool["name"] for tool in listing.json()["result"]["tools"]] == TOOL_NAMES
        assert call.status_code == 200
        assert "mcp-session-id" not in call.headers
        assert call.json()["result"]["isError"] is False

    async def test_unknown_methods_are_json_rpc_errors(self, gateway):
        response = await post(gateway, rpc("tools/explode"))

        assert response.json()["error"]["code"] == -32601

    async def test_unknown_tools_are_tool_errors(self, gateway, mode):
        async with open_client(gateway, mode=mode) as client:
            result = await client.call_tool("drop_database", {})

        assert result.is_error is True
        assert "drop_database" in text_of(result)

    async def test_the_modern_era_accepts_requests_without_a_handshake(self, gateway):
        headers = {"mcp-protocol-version": "2026-07-28", "mcp-method": "tools/list"}
        body = rpc("tools/list")
        body["params"] = {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {},
            }
        }

        response = await post(gateway, body, headers=headers)

        assert response.status_code == 200
        assert [tool["name"] for tool in response.json()["result"]["tools"]] == TOOL_NAMES


class TestNoStreamIsEverServed:
    @pytest.mark.parametrize(("method", "params", "name"), MODERN_REQUESTS)
    async def test_every_modern_request_gets_one_complete_json_document(
        self, gateway, method, params, name
    ):
        headers = {"mcp-protocol-version": MODERN_VERSION, "mcp-method": method}
        if name is not None:
            headers["mcp-name"] = name

        async with asyncio.timeout(RESPONSE_BUDGET):
            response = await post(
                gateway, rpc(method, {**params, "_meta": MODERN_META}), headers=headers
            )

        assert response.headers["content-type"].startswith("application/json")
        assert response.json()["jsonrpc"] == "2.0"
        assert response.json()["id"] == 1

    async def test_a_listen_request_is_an_unknown_method_and_never_an_event_stream(self, gateway):
        body = rpc(
            "subscriptions/listen",
            {"notifications": {"toolsListChanged": True}, "_meta": MODERN_META},
        )
        headers = {"mcp-protocol-version": MODERN_VERSION, "mcp-method": "subscriptions/listen"}

        async with asyncio.timeout(RESPONSE_BUDGET):
            response = await post(gateway, body, headers=headers)

        assert response.headers["content-type"].startswith("application/json")
        assert response.json()["error"]["code"] == METHOD_NOT_FOUND

    async def test_the_sdk_cannot_open_a_listen_stream_through_the_gateway(self, gateway):
        async with open_client(gateway, mode="auto") as client:
            assert client.protocol_version == MODERN_VERSION
            with pytest.raises(MCPError) as refused:
                async with client.listen(tools_list_changed=True):
                    pytest.fail("the gateway must not open a listen stream")
            listed = await client.list_tools()

        assert refused.value.code == METHOD_NOT_FOUND
        assert [tool.name for tool in listed.tools] == TOOL_NAMES

    @pytest.mark.parametrize("mode", ["legacy", "auto"])
    async def test_neither_era_announces_change_notifications(self, gateway, mode):
        async with open_client(gateway, mode=mode) as client:
            capabilities = client.server_capabilities

        assert capabilities.tools is not None
        assert capabilities.tools.list_changed is False
        assert capabilities.resources is not None
        assert (capabilities.resources.subscribe, capabilities.resources.list_changed) == (
            False,
            False,
        )
        assert capabilities.prompts is not None
        assert capabilities.prompts.list_changed is False


class TestToolListing:
    async def test_exactly_the_eleven_tools_of_the_contract_are_listed(self, gateway, mode):
        listed = await listed_tools(gateway, mode)

        assert [tool.name for tool in listed] == TOOL_NAMES

    @pytest.mark.parametrize("name", TOOL_NAMES)
    async def test_the_input_schema_matches_the_contract_table(self, gateway, mode, name):
        tool = next(item for item in await listed_tools(gateway, mode) if item.name == name)
        schema = tool.input_schema
        expected = EXPECTED_PARAMETERS[name]

        assert schema["type"] == "object"
        assert set(schema["properties"]) == set(expected)
        assert sorted(schema.get("required", [])) == sorted(
            key for key, (_, default) in expected.items() if default == REQUIRED
        )
        for key, (shape, default) in expected.items():
            property_schema = schema["properties"][key]
            assert shape_of(property_schema) == shape, key
            assert property_schema["description"], key
            if default == REQUIRED:
                assert "default" not in property_schema
            else:
                assert property_schema["default"] == default, key

    async def test_enumerations_and_numeric_bounds_are_part_of_the_schema(self, gateway):
        by_name = {tool.name: tool.input_schema for tool in await listed_tools(gateway, "legacy")}
        search = by_name["search_skills"]["properties"]

        assert search["kind"]["anyOf"][0]["enum"] == ["skill", "grounding"]
        assert (search["limit"]["minimum"], search["limit"]["maximum"]) == (1, 50)
        assert search["offset"]["minimum"] == 0
        assert search["offset"]["maximum"] == 2**31 - 1
        assert search["query"]["maxLength"] == 512
        for taxonomy in ("list_tags", "list_categories"):
            limit = by_name[taxonomy]["properties"]["limit"]
            assert (limit["minimum"], limit["maximum"]) == (1, 200)
            assert by_name[taxonomy]["properties"]["kind"]["anyOf"][0]["enum"] == [
                "skill",
                "grounding",
            ]

    async def test_every_tool_documents_itself_for_agents(self, gateway, mode):
        for tool in await listed_tools(gateway, mode):
            assert tool.title
            assert len(tool.description) > 200
            assert tool.annotations is not None
            assert tool.annotations.open_world_hint is False
            assert tool.output_schema is not None
            assert tool.output_schema["type"] == "object"

    async def test_read_tools_are_annotated_read_only_and_write_tools_are_not(self, gateway):
        listed = await listed_tools(gateway, "legacy")

        flags = {tool.name: tool.annotations.read_only_hint for tool in listed}
        assert flags == {name: name not in WRITE_TOOLS for name in TOOL_NAMES}

    async def test_output_schemas_describe_the_shared_dtos(self, gateway):
        outputs = {tool.name: tool.output_schema for tool in await listed_tools(gateway, "legacy")}

        assert set(outputs["search_skills"]["properties"]) == {"items", "total", "limit", "offset"}
        assert {"id", "name", "kind", "versions"} <= set(outputs["get_skill"]["properties"])
        assert {"name", "version", "kind", "content", "truncated"} <= set(
            outputs["get_entrypoint"]["properties"]
        )
        assert set(outputs["list_files"]["properties"]) == {"name", "version", "files"}
        assert set(outputs["get_download_info"]["properties"]) == {
            "name",
            "version",
            "sha256",
            "size_bytes",
            "url",
            "requires_auth",
        }
        assert set(outputs["upload_package"]["properties"]) == {"skill", "version"}
        assert set(outputs["list_tags"]["properties"]) == {"result"}
        assert {"status", "published_at", "deprecated_at"} <= set(
            outputs["publish_version"]["properties"]
        )


WRITE_TOOLS = {"upload_package", "publish_version", "deprecate_version"}

READ_CALLS = [
    ("search_skills", {}),
    ("search_skills", {"query": "pdf", "limit": 1}),
    ("search_skills", {"kind": "grounding", "tags": ["api"]}),
    ("get_skill", {"name": PDF}),
    ("get_skill", {"name": API}),
    ("get_entrypoint", {"name": PDF}),
    ("get_entrypoint", {"name": API, "version": "1.0.0"}),
    ("list_files", {"name": PDF}),
    ("list_files", {"name": API, "version": "latest"}),
    ("read_file", {"name": PDF, "path": "docs/guide.md"}),
    ("read_file", {"name": PDF, "path": "data/table.bin"}),
    ("read_file", {"name": PDF, "path": "docs/guide.md", "version": "1.0.0"}),
    ("get_download_info", {"name": PDF}),
    ("get_download_info", {"name": API, "version": "1.0.0"}),
    ("list_tags", {}),
    ("list_tags", {"kind": "skill", "limit": 2}),
    ("list_categories", {}),
]


@pytest.mark.usefixtures("registry")
class TestReadToolsOverTheWire:
    @pytest.mark.parametrize(("name", "arguments"), READ_CALLS)
    async def test_the_protocol_returns_exactly_what_the_tool_function_returns(
        self, gateway, mode, name, arguments
    ):
        direct = await getattr(tools, name)(**arguments)

        async with open_client(gateway, mode=mode) as client:
            remote = await client.call_tool(name, arguments)

        assert remote.is_error is False
        assert remote.structured_content == direct.structured_content
        assert text_of(remote) == text_of(direct)
        assert payload_of(remote) == payload_of(direct)

    async def test_the_same_call_gives_the_same_payload_in_every_protocol_era(self, gateway):
        payloads = []
        for era in MODES:
            async with open_client(gateway, mode=era) as client:
                result = await client.call_tool("get_skill", {"name": PDF})
            payloads.append(result.structured_content)

        assert payloads[0] == payloads[1] == payloads[2]

    async def test_a_realistic_agent_session_discovers_reads_and_follows_the_entry_point(
        self, gateway, mode
    ):
        async with open_client(gateway, mode=mode) as client:
            found = payload_of(await client.call_tool("search_skills", {"query": "forms"}))
            name = found["items"][0]["name"]
            entry = payload_of(await client.call_tool("get_entrypoint", {"name": name}))
            files = payload_of(await client.call_tool("list_files", {"name": name}))
            guide = payload_of(
                await client.call_tool("read_file", {"name": name, "path": "docs/guide.md"})
            )
            download = payload_of(await client.call_tool("get_download_info", {"name": name}))

        assert name == PDF
        assert entry["version"] == files["version"] == download["version"] == "1.1.0"
        assert "docs/guide.md" in [item["path"] for item in files["files"]]
        assert guide["content"].startswith("# Guide")
        assert download["url"].endswith("/api/v1/skills/pdf-forms/versions/1.1.0/download")

    async def test_service_errors_are_tool_errors_over_the_wire(self, gateway, mode):
        async with open_client(gateway, mode=mode) as client:
            missing = await client.call_tool("get_skill", {"name": "nope"})
            traversal = await client.call_tool(
                "read_file", {"name": PDF, "path": "../../etc/passwd"}
            )
            draft = await client.call_tool("get_entrypoint", {"name": PDF, "version": "2.0.0"})

        assert (missing.is_error, text_of(missing)) == (True, "NOT_FOUND: Skill not found: 'nope'")
        assert traversal.is_error is True
        assert re.fullmatch(r"VALIDATION_ERROR: .+ \[INVALID_PATH\]", text_of(traversal))
        assert text_of(draft).startswith("NOT_FOUND: Version not found")

    async def test_http_level_failures_are_not_used_for_domain_errors(self, gateway):
        response = await post(gateway, rpc_call("get_skill", {"name": "nope"}))

        assert response.status_code == 200
        assert response.json()["result"]["isError"] is True


class TestWriteToolsOverTheWire:
    async def test_a_writer_uploads_publishes_and_deprecates_through_the_protocol(
        self, gateway, credentials, mode
    ):
        first = make_skill_zip("wire-skill", "1.0.0", tags=["wire"])
        second = make_skill_zip("wire-skill", "1.1.0")
        async with open_client(gateway, credentials.owner_write, mode=mode) as client:
            uploaded = payload_of(
                await client.call_tool(
                    "upload_package", {"content_base64": zip_to_base64(first), "publish": True}
                )
            )
            draft = payload_of(
                await client.call_tool("upload_package", {"content_base64": zip_to_base64(second)})
            )
            published = payload_of(
                await client.call_tool(
                    "publish_version", {"name": "wire-skill", "version": "1.1.0"}
                )
            )
            deprecated = payload_of(
                await client.call_tool(
                    "deprecate_version", {"name": "wire-skill", "version": "1.0.0"}
                )
            )
            detail = payload_of(await client.call_tool("get_skill", {"name": "wire-skill"}))

        assert uploaded["version"]["status"] == "published"
        assert draft["version"]["status"] == "draft"
        assert published["status"] == "published"
        assert deprecated["status"] == "deprecated"
        assert detail["latest_version"] == "1.1.0"
        assert [item["status"] for item in detail["versions"]] == ["published", "deprecated"]

    async def test_a_grounding_can_be_uploaded_and_read_through_the_protocol(
        self, gateway, credentials
    ):
        data = make_grounding_zip("wire-ground", "1.0.0", files={"tables/a.csv": "a,b\n1,2\n"})
        async with open_client(gateway, credentials.owner_write) as client:
            await client.call_tool(
                "upload_package", {"content_base64": zip_to_base64(data), "publish": True}
            )
            entry = payload_of(await client.call_tool("get_entrypoint", {"name": "wire-ground"}))
            table = payload_of(
                await client.call_tool("read_file", {"name": "wire-ground", "path": "tables/a.csv"})
            )

        assert (entry["kind"], entry["path"]) == ("grounding", "INDEX.md")
        assert table["content"] == "a,b\n1,2\n"

    @pytest.mark.usefixtures("registry")
    async def test_results_are_valid_json_for_every_tool(self, gateway, credentials):
        async with open_client(gateway, credentials.owner_write) as client:
            for name, arguments in READ_CALLS:
                result = await client.call_tool(name, arguments)
                json.loads(text_of(result))
