from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.metadata
import json
import os
import random
import signal
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest
from mcp import Client, MCPError
from mcp.client.streamable_http import streamable_http_client
from mcp.types import METHOD_NOT_FOUND

from app.cli import LoadedPackage, pack_directory
from tests.helpers.live import (
    ROOT,
    LiveServer,
    clean_environment,
    spawn_group,
    start_server,
    terminate_group,
    wait_until,
)
from tests.helpers.packages import make_skill_zip

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the live server tests need POSIX")

EXAMPLES = ROOT / "examples" / "packages"
CALL_TIMEOUT = 60
API = "/api/v1"
ACCEPT = "application/json, text/event-stream"
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
UNKNOWN_TOKEN = "alx_" + "z" * 43
MODERN_VERSION = "2026-07-28"
LISTEN_REQUEST = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "subscriptions/listen",
    "params": {
        "notifications": {"toolsListChanged": True},
        "_meta": {
            "io.modelcontextprotocol/protocolVersion": MODERN_VERSION,
            "io.modelcontextprotocol/clientCapabilities": {},
        },
    },
}
LISTEN_HEADERS = {
    "accept": ACCEPT,
    "mcp-protocol-version": MODERN_VERSION,
    "mcp-method": "subscriptions/listen",
}
LISTEN_ATTEMPTS = 60
LISTEN_BUDGET = 30
STOP_GRACE = 10.0
ORPHAN_SCRIPT = (
    "import os, sys\n"
    "from pathlib import Path\n"
    "from tests.helpers.live import start_server\n"
    "server = start_server(Path(sys.argv[1]))\n"
    "print(server.process.pid, flush=True)\n"
    "os._exit(1)\n"
)


@dataclass(frozen=True)
class Example:
    kind: str
    entrypoint: str
    text_file: str


EXAMPLE_FILES = {
    "changelog-from-commits": Example("skill", "SKILL.md", "scripts/format_changelog.py"),
    "http-reference": Example("grounding", "INDEX.md", "tables/status_codes.csv"),
    "incident-postmortem": Example("skill", "SKILL.md", "templates/postmortem.html"),
}
EXAMPLE_NAMES = list(EXAMPLE_FILES)
BINARY_EXAMPLE = ("http-reference", "db/http-reference.sqlite")


@dataclass
class Journey:
    server: LiveServer
    admin_id: str
    admin_jwt: str = field(repr=False)
    write_token: str = field(repr=False)
    read_token: str = field(repr=False)
    uploads: dict[str, dict[str, Any]]
    packed: dict[str, LoadedPackage]


def bearer(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def source_bytes(name: str, path: str) -> bytes:
    return (EXAMPLES / name / path).read_bytes()


def build_journey(server: LiveServer) -> Journey:
    with server.http() as http:
        login = http.post(
            f"{API}/auth/login",
            json={"username": server.admin_username, "password": server.admin_password},
        )
        assert login.status_code == 200, login.text
        admin_jwt = login.json()["token"]
        admin_id = login.json()["user"]["id"]

        def issue(name: str, scope: str) -> str:
            created = http.post(
                f"{API}/tokens",
                json={"name": name, "scope": scope},
                headers=bearer(admin_jwt),
            )
            assert created.status_code == 201, created.text
            return created.json()["token"]

        write_token = issue("journey-write", "write")
        read_token = issue("journey-read", "read")
    uploads: dict[str, dict[str, Any]] = {}
    packed: dict[str, LoadedPackage] = {}
    for name in EXAMPLE_NAMES:
        result = server.alx("publish", EXAMPLES / name, "--publish", "--json", token=write_token)
        assert result.returncode == 0, result.stderr
        uploads[name] = result.json()
        packed[name] = pack_directory(EXAMPLES / name)
    return Journey(server, admin_id, admin_jwt, write_token, read_token, uploads, packed)


@pytest.fixture(scope="module")
def journey(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Journey]:
    server = start_server(tmp_path_factory.mktemp("live-journey"))
    try:
        yield build_journey(server)
    finally:
        server.stop()


@asynccontextmanager
async def connect(
    server: LiveServer, token: str | None = None, mode: str = "legacy"
) -> AsyncIterator[Client]:
    async with (
        asyncio.timeout(CALL_TIMEOUT),
        httpx2.AsyncClient(headers=bearer(token), timeout=CALL_TIMEOUT, trust_env=False) as http,
        Client(streamable_http_client(server.mcp_url, http_client=http), mode=mode) as client,
    ):
        yield client


def payload(result: Any) -> Any:
    assert len(result.content) == 1
    return json.loads(result.content[0].text)


async def get_json(server: LiveServer, path: str, token: str | None = None) -> Any:
    async with httpx.AsyncClient(
        base_url=server.url, headers=bearer(token), timeout=CALL_TIMEOUT, trust_env=False
    ) as http:
        response = await http.get(f"{API}{path}")
    assert response.status_code == 200, response.text
    return response.json()


def leaves(error: BaseException) -> list[BaseException]:
    if isinstance(error, BaseExceptionGroup):
        return [leaf for inner in error.exceptions for leaf in leaves(inner)]
    return [error]


class TestTheServerStartsAndPublishes:
    def test_the_server_runs_the_migrated_schema_and_describes_itself(self, journey):
        with journey.server.http() as http:
            health = http.get(f"{API}/health")
            info = http.get(f"{API}/info").json()

        assert health.json() == {"status": "ok"}
        assert info["name"] == "Alexandria"
        assert info["version"] == importlib.metadata.version("alexandria")
        assert info["public_url"] == journey.server.url
        assert info["mcp_path"] == "/mcp"
        assert info["public_read"] is True

    def test_the_admin_can_log_in_and_the_jwt_and_the_tokens_all_identify_the_same_user(
        self, journey
    ):
        with journey.server.http() as http:
            identities = {
                name: http.get(f"{API}/auth/me", headers=bearer(token))
                for name, token in (
                    ("jwt", journey.admin_jwt),
                    ("write", journey.write_token),
                    ("read", journey.read_token),
                )
            }

        for name, response in identities.items():
            assert response.status_code == 200, name
            assert response.json()["id"] == journey.admin_id
            assert response.json()["role"] == "admin"

    @pytest.mark.parametrize("name", EXAMPLE_NAMES)
    def test_alx_publish_stored_each_example_exactly_as_packed_locally(self, journey, name):
        upload = journey.uploads[name]
        packed = journey.packed[name]

        assert upload["skill"]["name"] == name
        assert upload["skill"]["kind"] == EXAMPLE_FILES[name].kind
        assert upload["skill"]["owner_id"] == journey.admin_id
        assert upload["version"]["version"] == "1.0.0"
        assert upload["version"]["status"] == "published"
        assert upload["version"]["sha256"] == packed.parsed.sha256
        assert upload["version"]["size_bytes"] == len(packed.data)
        assert upload["version"]["file_count"] == packed.parsed.file_count

    def test_publishing_the_same_version_again_is_a_conflict_that_changes_nothing(self, journey):
        again = journey.server.alx(
            "publish", EXAMPLES / "http-reference", "--publish", "--json", token=journey.write_token
        )

        assert again.returncode == 1
        assert json.loads(again.stderr)["error"]["code"] == "CONFLICT"


class TestRestSearchesReadsAndDownloads:
    def test_the_published_examples_are_found_by_an_anonymous_search(self, journey):
        with journey.server.http() as http:
            everything = http.get(f"{API}/skills", params={"limit": 100}).json()
            grounding = http.get(f"{API}/skills", params={"kind": "grounding"}).json()
            tagged = http.get(f"{API}/skills", params={"tag": "git"}).json()
            by_category = http.get(f"{API}/skills", params={"category": "operations"}).json()
            words = http.get(f"{API}/skills", params={"q": "blameless postmortem"}).json()

        assert set(EXAMPLE_NAMES) <= {item["name"] for item in everything["items"]}
        assert [item["name"] for item in grounding["items"]] == ["http-reference"]
        assert [item["name"] for item in tagged["items"]] == ["changelog-from-commits"]
        assert [item["name"] for item in by_category["items"]] == ["incident-postmortem"]
        assert [item["name"] for item in words["items"]] == ["incident-postmortem"]

    @pytest.mark.parametrize("name", EXAMPLE_NAMES)
    def test_the_entry_the_manifest_and_the_files_match_the_local_package(self, journey, name):
        packed = journey.packed[name]
        example = EXAMPLE_FILES[name]

        with journey.server.http() as http:
            detail = http.get(f"{API}/skills/{name}").json()
            entrypoint = http.get(f"{API}/skills/{name}/versions/latest/entrypoint").json()
            listing = http.get(f"{API}/skills/{name}/versions/1.0.0/files").json()
            text = http.get(f"{API}/skills/{name}/versions/1.0.0/files/{example.text_file}").json()
            raw = http.get(f"{API}/skills/{name}/versions/1.0.0/raw/{example.text_file}")

        assert (detail["kind"], detail["status"], detail["latest_version"]) == (
            example.kind,
            "published",
            "1.0.0",
        )
        assert [item["version"] for item in detail["versions"]] == ["1.0.0"]
        assert entrypoint["path"] == example.entrypoint
        assert entrypoint["content"].encode("utf-8") == source_bytes(name, example.entrypoint)
        assert listing["files"] == [item.to_dict() for item in packed.parsed.manifest]
        assert text["content"].encode("utf-8") == source_bytes(name, example.text_file)
        assert raw.content == source_bytes(name, example.text_file)
        assert raw.headers["content-type"] == "text/plain; charset=utf-8"
        assert raw.headers["x-content-type-options"] == "nosniff"
        assert raw.headers["content-security-policy"] == "default-src 'none'; sandbox"

    def test_a_binary_file_travels_byte_for_byte_through_the_raw_endpoint(self, journey):
        name, path = BINARY_EXAMPLE

        with journey.server.http() as http:
            raw = http.get(f"{API}/skills/{name}/versions/latest/raw/{path}")
            metadata = http.get(f"{API}/skills/{name}/versions/latest/files/{path}").json()

        original = source_bytes(name, path)
        assert raw.status_code == 200
        assert raw.content == original
        assert raw.headers["content-type"] == "application/octet-stream"
        assert raw.headers["content-length"] == str(len(original))
        assert metadata["text"] is False
        assert metadata["content"] is None
        assert metadata["sha256"] == hashlib.sha256(original).hexdigest()

    @pytest.mark.parametrize("name", EXAMPLE_NAMES)
    def test_the_download_is_the_published_zip_and_its_hash_matches_everywhere(self, journey, name):
        packed = journey.packed[name]

        with journey.server.http() as http:
            latest = http.get(f"{API}/skills/{name}/versions/latest/download")
            pinned = http.get(f"{API}/skills/{name}/versions/1.0.0/download")
            revalidated = http.get(
                f"{API}/skills/{name}/versions/1.0.0/download",
                headers={"If-None-Match": pinned.headers["etag"]},
            )

        digest = hashlib.sha256(pinned.content).hexdigest()
        assert pinned.status_code == latest.status_code == 200
        assert pinned.content == latest.content == packed.data
        assert digest == packed.parsed.sha256
        assert pinned.headers["x-content-sha256"] == digest
        assert pinned.headers["etag"] == latest.headers["etag"] == f'"{digest}"'
        assert pinned.headers["content-type"] == "application/zip"
        assert pinned.headers["content-disposition"].startswith(
            f'attachment; filename="{name}-1.0.0.zip"'
        )
        assert pinned.headers["cache-control"] == "public, immutable"
        assert latest.headers["cache-control"] == "public, no-cache"
        assert revalidated.status_code == 304

    def test_alx_pulls_what_it_published_and_verifies_it(self, journey, tmp_path):
        found = journey.server.alx("search", "--json", "--kind", "grounding")
        pulled = journey.server.alx("pull", "http-reference", "-d", tmp_path / "refs")

        assert found.returncode == 0, found.stderr
        assert [item["name"] for item in found.json()["items"]] == ["http-reference"]
        assert pulled.returncode == 0, pulled.stderr
        for path in ("INDEX.md", "tables/status_codes.csv", BINARY_EXAMPLE[1]):
            assert (tmp_path / "refs" / path).read_bytes() == source_bytes("http-reference", path)


class TestMcpOverTheRealSocket:
    async def test_the_client_initializes_and_sees_the_eleven_tools(self, journey):
        async with connect(journey.server) as client:
            tools = await client.list_tools()

            assert client.server_info is not None
            assert client.server_info.name == "alexandria"
            assert client.server_info.version == importlib.metadata.version("alexandria")
            assert "SKILL.md" in (client.instructions or "")
        assert [tool.name for tool in tools.tools] == TOOL_NAMES

    @pytest.mark.parametrize("mode", ["legacy", "auto"])
    async def test_both_negotiation_modes_reach_the_same_server(self, journey, mode):
        async with connect(journey.server, mode=mode) as client:
            result = await client.call_tool("search_skills", {"query": "http", "kind": "grounding"})

        assert [item["name"] for item in payload(result)["items"]] == ["http-reference"]

    @pytest.mark.parametrize("name", EXAMPLE_NAMES)
    async def test_an_agent_can_search_read_and_download_a_package(self, journey, name):
        packed = journey.packed[name]
        example = EXAMPLE_FILES[name]

        async with connect(journey.server) as client:
            found = payload(await client.call_tool("search_skills", {"query": name}))
            entrypoint = payload(await client.call_tool("get_entrypoint", {"name": name}))
            listing = payload(await client.call_tool("list_files", {"name": name}))
            text = payload(
                await client.call_tool("read_file", {"name": name, "path": example.text_file})
            )
            info = payload(await client.call_tool("get_download_info", {"name": name}))
        async with httpx.AsyncClient(timeout=CALL_TIMEOUT, trust_env=False) as http:
            download = await http.get(info["url"])

        assert [item["name"] for item in found["items"]] == [name]
        assert entrypoint["content"].encode("utf-8") == source_bytes(name, example.entrypoint)
        assert (entrypoint["name"], entrypoint["version"], entrypoint["kind"]) == (
            name,
            "1.0.0",
            example.kind,
        )
        assert listing["files"] == [item.to_dict() for item in packed.parsed.manifest]
        assert text["content"].encode("utf-8") == source_bytes(name, example.text_file)
        assert info["url"] == f"{journey.server.url}{API}/skills/{name}/versions/1.0.0/download"
        assert info["requires_auth"] is False
        assert info["sha256"] == packed.parsed.sha256
        assert info["size_bytes"] == len(packed.data)
        assert download.status_code == 200
        assert hashlib.sha256(download.content).hexdigest() == info["sha256"]
        assert download.content == packed.data

    async def test_mcp_and_rest_describe_the_same_registry(self, journey):
        name = "http-reference"
        async with connect(journey.server) as client:
            detail = payload(await client.call_tool("get_skill", {"name": name}))
            tags = payload(await client.call_tool("list_tags", {}))
            categories = payload(await client.call_tool("list_categories", {}))
        over_rest = {
            "detail": await get_json(journey.server, f"/skills/{name}"),
            "tags": await get_json(journey.server, "/tags"),
            "categories": await get_json(journey.server, "/categories"),
        }

        assert detail == over_rest["detail"]
        assert tags == over_rest["tags"]
        assert categories == over_rest["categories"]

    async def test_a_binary_file_is_described_over_mcp_and_only_downloaded_over_http(self, journey):
        name, path = BINARY_EXAMPLE
        async with connect(journey.server) as client:
            result = payload(await client.call_tool("read_file", {"name": name, "path": path}))

        original = source_bytes(name, path)
        assert result["text"] is False
        assert result["content"] is None
        assert result["size"] == len(original)
        assert result["sha256"] == hashlib.sha256(original).hexdigest()

    async def test_a_missing_entry_is_a_tool_error_with_the_domain_code(self, journey):
        async with connect(journey.server) as client:
            result = await client.call_tool("get_skill", {"name": "no-such-entry"})

        assert result.is_error is True
        assert result.content[0].text.startswith("NOT_FOUND: ")

    @pytest.mark.parametrize("mode", ["legacy", "auto"])
    async def test_the_server_announces_no_change_notifications_in_either_era(self, journey, mode):
        async with connect(journey.server, mode=mode) as client:
            capabilities = client.server_capabilities

        assert capabilities.tools is not None
        assert capabilities.tools.list_changed is False
        assert capabilities.prompts is not None
        assert capabilities.prompts.list_changed is False
        assert capabilities.resources is not None
        assert capabilities.resources.list_changed is False
        assert capabilities.resources.subscribe is False

    async def test_listen_requests_are_refused_at_once_and_never_open_a_stream(self, journey):
        async with connect(journey.server, mode="auto") as client:
            assert client.protocol_version == MODERN_VERSION
            with pytest.raises(MCPError) as refused:
                async with client.listen(tools_list_changed=True):
                    pytest.fail("the server must not open a listen stream")
            listed = await client.list_tools()
        async with httpx.AsyncClient(timeout=CALL_TIMEOUT, trust_env=False) as http:
            async with asyncio.timeout(LISTEN_BUDGET):
                answers = await asyncio.gather(
                    *(
                        http.post(
                            journey.server.mcp_url, json=LISTEN_REQUEST, headers=LISTEN_HEADERS
                        )
                        for _ in range(LISTEN_ATTEMPTS)
                    )
                )

        assert refused.value.code == METHOD_NOT_FOUND
        assert [tool.name for tool in listed.tools] == TOOL_NAMES
        assert len(answers) == LISTEN_ATTEMPTS
        for answer in answers:
            assert answer.headers["content-type"].startswith("application/json")
            assert answer.json()["error"]["code"] == METHOD_NOT_FOUND


class TestThePublicSite:
    def test_the_landing_page_and_the_registry_list_what_was_published(self, journey):
        with journey.server.http() as http:
            landing = http.get("/")
            registry = http.get("/registry")
            recent = http.get(f"{API}/skills", params={"sort": "updated", "limit": 6}).json()

        assert landing.status_code == registry.status_code == 200
        for page in (landing, registry):
            assert page.headers["content-type"].startswith("text/html")
        assert "content-security-policy" in registry.headers
        for item in recent["items"]:
            assert f'href="/registry/skills/{item["name"]}"' in landing.text
        for name in EXAMPLE_NAMES:
            assert f'href="/registry/skills/{name}"' in registry.text

    @pytest.mark.parametrize("name", EXAMPLE_NAMES)
    def test_each_package_has_a_public_page_that_shows_its_version(self, journey, name):
        with journey.server.http() as http:
            page = http.get(f"/registry/skills/{name}")
            filtered = http.get("/registry", params={"q": name})

        assert page.status_code == 200
        assert "1.0.0" in page.text
        assert f'href="/registry/skills/{name}"' in filtered.text

    def test_an_unknown_page_is_a_404_page_not_a_json_error(self, journey):
        with journey.server.http() as http:
            response = http.get("/registry/skills/no-such-entry")

        assert response.status_code == 404
        assert response.headers["content-type"].startswith("text/html")


class TestWritePermissionsOverTheRealSocket:
    async def test_a_read_token_reads_but_cannot_upload_publish_or_deprecate(self, journey):
        data = make_skill_zip("read-token-attempt", "1.0.0")
        arguments = {"content_base64": base64.b64encode(data).decode("ascii"), "publish": True}

        async with connect(journey.server, journey.read_token) as client:
            readable = await client.call_tool("get_skill", {"name": "http-reference"})
            upload = await client.call_tool("upload_package", arguments)
            publish = await client.call_tool(
                "publish_version", {"name": "http-reference", "version": "1.0.0"}
            )
            deprecate = await client.call_tool(
                "deprecate_version", {"name": "http-reference", "version": "1.0.0"}
            )

        assert readable.is_error is False
        for denied in (upload, publish, deprecate):
            assert denied.is_error is True
            assert denied.content[0].text.startswith("FORBIDDEN: ")
        with journey.server.http() as http:
            assert http.get(f"{API}/skills/read-token-attempt").status_code == 404
            over_rest = http.post(
                f"{API}/packages",
                headers=bearer(journey.read_token),
                files={"file": ("p.zip", data, "application/zip")},
            )
        assert over_rest.status_code == 403

    async def test_a_write_token_uploads_publishes_and_deprecates_over_mcp(self, journey):
        first = make_skill_zip("live-mcp-writer", "1.0.0", files={"docs/a.md": "first\n"})
        second = make_skill_zip("live-mcp-writer", "1.1.0", files={"docs/a.md": "second\n"})

        async with connect(journey.server, journey.write_token) as client:
            created = payload(
                await client.call_tool(
                    "upload_package",
                    {"content_base64": base64.b64encode(first).decode("ascii"), "publish": True},
                )
            )
            draft = payload(
                await client.call_tool(
                    "upload_package", {"content_base64": base64.b64encode(second).decode("ascii")}
                )
            )
            anonymous_before = await get_json(journey.server, "/skills/live-mcp-writer")
            published = payload(
                await client.call_tool(
                    "publish_version", {"name": "live-mcp-writer", "version": "1.1.0"}
                )
            )
            deprecated = payload(
                await client.call_tool(
                    "deprecate_version", {"name": "live-mcp-writer", "version": "1.0.0"}
                )
            )
        latest = await get_json(
            journey.server, "/skills/live-mcp-writer/versions/latest/files/docs/a.md"
        )

        assert created["version"]["status"] == "published"
        assert created["version"]["sha256"] == hashlib.sha256(first).hexdigest()
        assert draft["version"]["status"] == "draft"
        assert [item["version"] for item in anonymous_before["versions"]] == ["1.0.0"]
        assert published["status"] == "published"
        assert deprecated["status"] == "deprecated"
        assert latest["content"] == "second\n"

    async def test_a_large_package_goes_through_mcp_for_a_writer_and_is_cut_off_for_a_reader(
        self, journey
    ):
        noise = random.Random(31).randbytes(5 * 1024 * 1024)
        data = make_skill_zip("live-large-mcp", "1.0.0", files={"noise.bin": noise})
        content = base64.b64encode(data).decode("ascii")
        request = {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": "upload_package", "arguments": {"content_base64": content}},
        }

        async with connect(journey.server, journey.write_token) as client:
            uploaded = payload(
                await client.call_tool(
                    "upload_package", {"content_base64": content, "publish": True}
                )
            )
        async with httpx.AsyncClient(timeout=CALL_TIMEOUT, trust_env=False) as http:
            try:
                outcome: object = (
                    await http.post(
                        journey.server.mcp_url,
                        json=request,
                        headers={"accept": ACCEPT, **bearer(journey.read_token)},
                    )
                ).status_code
            except httpx.TransportError as error:
                outcome = type(error)
            download = await http.get(
                f"{journey.server.url}{API}/skills/live-large-mcp/versions/1.0.0/download"
            )

        assert uploaded["version"]["size_bytes"] == len(data)
        assert uploaded["version"]["status"] == "published"
        assert download.content == data
        assert outcome == 413 or (
            isinstance(outcome, type) and issubclass(outcome, httpx.TransportError)
        )

    def test_a_large_package_goes_through_rest_and_comes_back_intact(self, journey):
        noise = random.Random(32).randbytes(5 * 1024 * 1024)
        data = make_skill_zip("live-large-rest", "1.0.0", files={"noise.bin": noise})

        with journey.server.http(journey.write_token) as http:
            created = http.post(
                f"{API}/packages",
                files={"file": ("p.zip", data, "application/zip")},
                data={"publish": "true"},
            )
            download = http.get(f"{API}/skills/live-large-rest/versions/1.0.0/download")
            raw = http.get(f"{API}/skills/live-large-rest/versions/1.0.0/raw/noise.bin")

        assert created.status_code == 201
        assert download.content == data
        assert raw.content == noise

    async def test_what_rest_uploads_mcp_reads_back(self, journey):
        data = make_skill_zip("live-rest-writer", "2.0.0", files={"docs/b.md": "from rest\n"})
        with journey.server.http(journey.write_token) as http:
            created = http.post(
                f"{API}/packages",
                files={"file": ("p.zip", data, "application/zip")},
                data={"publish": "true"},
            )

        async with connect(journey.server) as client:
            info = payload(
                await client.call_tool("get_download_info", {"name": "live-rest-writer"})
            )
            content = payload(
                await client.call_tool(
                    "read_file", {"name": "live-rest-writer", "path": "docs/b.md"}
                )
            )

        assert created.status_code == 201
        assert info["sha256"] == hashlib.sha256(data).hexdigest()
        assert content["content"] == "from rest\n"


class TestCredentialsAndTheEndpointShape:
    def test_an_invalid_credential_is_a_401_with_a_challenge_over_rest_and_mcp(self, journey):
        rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        with journey.server.http(UNKNOWN_TOKEN) as http:
            over_rest = http.get(f"{API}/auth/me")
            listing = http.get(f"{API}/skills")
            over_mcp = http.post("/mcp", json=rpc, headers={"accept": ACCEPT})

        for response in (over_rest, listing, over_mcp):
            assert response.status_code == 401
            assert response.headers["www-authenticate"] == "Bearer"
            assert response.json()["error"]["code"] == "UNAUTHORIZED"

    async def test_the_sdk_client_cannot_connect_with_an_invalid_credential(self, journey):
        with pytest.raises(BaseExceptionGroup) as raised:
            async with connect(journey.server, UNKNOWN_TOKEN) as client:
                await client.list_tools()

        failures = leaves(raised.value)
        assert failures
        assert all(isinstance(failure, MCPError) for failure in failures)

    def test_a_malformed_authorization_header_is_a_401_too(self, journey):
        with journey.server.http() as http:
            response = http.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                headers={"accept": ACCEPT, "authorization": "Basic dXNlcjpwYXNz"},
            )

        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"

    @pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
    def test_posting_to_the_endpoint_works_with_or_without_the_trailing_slash(self, journey, path):
        with journey.server.http() as http:
            response = http.post(
                path,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                headers={"accept": ACCEPT},
            )

        assert response.status_code == 200
        assert response.history == []
        assert [tool["name"] for tool in response.json()["result"]["tools"]] == TOOL_NAMES

    @pytest.mark.parametrize("method", ["GET", "DELETE", "PUT"])
    def test_the_endpoint_only_accepts_post(self, journey, method):
        with journey.server.http() as http:
            response = http.request(method, "/mcp", headers={"accept": ACCEPT})

        assert response.status_code == 405
        assert response.headers["allow"] == "POST"

    def test_the_mcp_traffic_is_logged_by_the_application_without_the_sdk_noise(self, journey):
        with journey.server.http() as http:
            for _ in range(3):
                http.post(
                    "/mcp",
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                    headers={"accept": ACCEPT},
                )

        log = journey.server.log()
        assert '"message": "POST /mcp -> 200"' in log
        assert "Terminating session" not in log


class TestTheServerLifecycle:
    def test_a_graceful_stop_shuts_mcp_down_before_the_database_and_leaves_no_errors(
        self, tmp_path
    ):
        server = start_server(tmp_path / "lifecycle")
        try:
            with server.http() as http:
                answered = http.post(
                    "/mcp",
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                    headers={"accept": ACCEPT},
                )
            code = server.stop()
        finally:
            server.stop()

        log = server.log()
        assert answered.status_code == 200
        assert code in {0, -signal.SIGTERM}
        assert not server.running()
        assert "Application startup complete" in log
        assert "Application shutdown complete" in log
        assert (
            log.index("Database initialized")
            < log.index("StreamableHTTP session manager started")
            < log.index("StreamableHTTP session manager shutting down")
            < log.index("Shutting down Alexandria")
        )
        served = log[log.index("Database initialized") :]
        records = [json.loads(line) for line in served.splitlines() if line.startswith("{")]
        assert {record["level"] for record in records} <= {"INFO", "WARNING"}
        assert "Traceback" not in served

    async def test_a_client_that_asked_to_listen_does_not_keep_the_server_from_stopping(
        self, tmp_path
    ):
        server = start_server(tmp_path / "listener")
        refusal: MCPError | None = None
        code: int | None = None
        try:
            async with connect(server, mode="auto") as client, AsyncExitStack() as streams:
                try:
                    await streams.enter_async_context(client.listen(tools_list_changed=True))
                except MCPError as error:
                    refusal = error
                code = await asyncio.to_thread(server.stop, STOP_GRACE)
        finally:
            server.stop()

        log = server.log()
        assert code in {0, -signal.SIGTERM}
        assert not server.running()
        assert "Waiting for connections to close" not in log
        assert "Application shutdown complete" in log
        assert "Traceback" not in log[log.index("Shutting down") :]
        assert refusal is not None
        assert refusal.code == METHOD_NOT_FOUND


class TestTheHarnessCleansUp:
    def test_stopping_a_group_kills_the_children_that_outlive_their_leader(self, tmp_path):
        log = tmp_path / "group.log"
        process = spawn_group(
            ["sh", "-c", "sleep 300 & echo $!"], clean_environment(), log, cwd=tmp_path
        )
        try:
            assert wait_until(lambda: log.read_text().strip() != "", 10)
            child = int(log.read_text().split()[0])
            assert wait_until(lambda: process.poll() is not None, 10)
            assert process_exists(child)

            terminate_group(process, grace=5)

            assert wait_until(lambda: not process_exists(child), 10)
        finally:
            terminate_group(process, grace=1)

    def test_a_leader_that_ignores_termination_is_killed_after_the_grace_period(self, tmp_path):
        script = "trap '' TERM; sleep 300 & echo $!; wait"
        log = tmp_path / "stubborn.log"
        process = spawn_group(["sh", "-c", script], clean_environment(), log, cwd=tmp_path)
        try:
            assert wait_until(lambda: log.read_text().strip() != "", 10)
            child = int(log.read_text().split()[0])

            code = terminate_group(process, grace=0.5)

            assert code == -signal.SIGKILL
            assert wait_until(lambda: not process_exists(child), 10)
        finally:
            terminate_group(process, grace=1)

    @pytest.mark.skipif(sys.platform != "linux", reason="needs PR_SET_PDEATHSIG")
    def test_a_server_does_not_outlive_a_parent_that_dies_without_cleaning_up(self, tmp_path):
        launched = subprocess.run(
            [sys.executable, "-c", ORPHAN_SCRIPT, str(tmp_path / "orphan")],
            cwd=ROOT,
            env=clean_environment(),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        pid = int(launched.stdout.split()[-1])

        assert launched.returncode == 1, launched.stderr
        assert wait_until(lambda: not process_exists(pid), 20)

    def test_stopping_twice_is_harmless(self, tmp_path):
        process = subprocess.Popen(["sleep", "300"], start_new_session=True)
        try:
            first = terminate_group(process, grace=5)
            second = terminate_group(process, grace=5)
        finally:
            terminate_group(process, grace=1)

        assert first == second == -signal.SIGTERM


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        status = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return True
    return status.rsplit(")", 1)[1].split()[0] != "Z"
