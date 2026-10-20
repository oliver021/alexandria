from __future__ import annotations

import hashlib
import json
import random
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, unquote

import pytest
from httpx import Response
from sqlalchemy import update

from app.config.settings import settings
from app.models import ApiToken
from app.repositories import user_repo
from app.services.errors import ForbiddenError, NotFoundError, UnauthorizedError
from app.services.token_service import TokenService
from tests.helpers.factories import ACTOR_NAMES, PRIVILEGED_ACTORS, write_outcome
from tests.helpers.packages import make_grounding_zip, make_skill_zip, make_zip, skill_md
from tests.test_integration.support import (
    API_REFERENCE,
    BINARY_PATH,
    DRAFT,
    EMPTY_PATH,
    FOREIGN,
    GUIDE_PATH,
    HIDDEN,
    LONG_PATH,
    MISSING,
    PUBLISHED,
    STATUS_OF_CODE,
    UNICODE_PATH,
    UNICODE_TEXT,
    Mcp,
    McpConnector,
    Rest,
    ToolOutcome,
    World,
    differences,
    issue_jwt,
    mcp_outcome,
    path_of_url,
    rest_outcome,
)

ALL = frozenset(ACTOR_NAMES)
PRIVILEGED = frozenset(PRIVILEGED_ACTORS)
NOBODY: frozenset[str] = frozenset()
WRITERS = frozenset({"owner", "stranger", "admin"})
EXTRA_ACTORS = ("owner_jwt", "invalid_token", "invalid_jwt")
MATRIX_ACTORS = (*ACTOR_NAMES, *EXTRA_ACTORS)
ERROR_CODES = {
    UnauthorizedError: "UNAUTHORIZED",
    ForbiddenError: "FORBIDDEN",
    NotFoundError: "NOT_FOUND",
}
PRIVATE_CACHE_CONTROL = "private, no-store"

Fetch = Callable[[Rest, str], Awaitable[tuple[Response, Any]]]


def describe(error: Mapping[str, Any]) -> str:
    text = f"{error['code']}: {error['message']}"
    return text + (f" [{error['reason']}]" if error.get("reason") else "")


@dataclass(frozen=True)
class Target:
    ref: str
    version: str
    visible_to: frozenset[str]
    file: str


@dataclass(frozen=True)
class Read:
    id: str
    tool: str
    arguments: Mapping[str, Any]
    visible_to: frozenset[str]
    fetch: Fetch


@dataclass
class Report:
    problems: list[str] = field(default_factory=list)
    successes: dict[str, set[str]] = field(default_factory=dict)


TARGETS = (
    Target(PUBLISHED, "latest", ALL, GUIDE_PATH),
    Target(PUBLISHED, "1.1.0", ALL, GUIDE_PATH),
    Target(PUBLISHED, "0.9.0", ALL, GUIDE_PATH),
    Target(PUBLISHED, "2.0.0-beta.1", ALL, GUIDE_PATH),
    Target(PUBLISHED, "2.0.0", PRIVILEGED, GUIDE_PATH),
    Target(PUBLISHED, "9.9.9", NOBODY, GUIDE_PATH),
    Target(f"id:{PUBLISHED}", "latest", ALL, GUIDE_PATH),
    Target(API_REFERENCE, "latest", ALL, "tables/codes.csv"),
    Target(DRAFT, "latest", NOBODY, "SKILL.md"),
    Target(DRAFT, "0.1.0", PRIVILEGED, "SKILL.md"),
    Target(HIDDEN, "latest", PRIVILEGED, "INDEX.md"),
    Target(HIDDEN, "1.0.0", PRIVILEGED, "INDEX.md"),
    Target(FOREIGN, "1.0.0", ALL, "SKILL.md"),
    Target(MISSING, "latest", NOBODY, "SKILL.md"),
)
ENTRIES = (
    (PUBLISHED, ALL),
    (f"id:{PUBLISHED}", ALL),
    (API_REFERENCE, ALL),
    (FOREIGN, ALL),
    (DRAFT, PRIVILEGED),
    (f"id:{DRAFT}", PRIVILEGED),
    (HIDDEN, PRIVILEGED),
    (MISSING, NOBODY),
)
SEARCHES = {
    "everything": {},
    "one-word": {"query": "pdf"},
    "two-words": {"query": "fill flatten"},
    "case-insensitive-text": {"query": "BODY OF FOREIGN"},
    "no-match": {"query": "zzz-nothing-matches"},
    "kind-skill": {"kind": "skill"},
    "kind-grounding": {"kind": "grounding"},
    "one-tag": {"tags": ["beta"]},
    "two-tags": {"tags": ["alpha", "beta"]},
    "category": {"category": "ops"},
    "combined": {"query": "pdf", "kind": "skill", "tags": ["beta"], "category": "ops"},
    "smallest-page": {"limit": 1},
    "first-page": {"limit": 2},
    "second-page": {"limit": 2, "offset": 2},
    "past-the-end": {"limit": 5, "offset": 100},
    "largest-page": {"limit": 50},
}
TAXONOMIES = {
    "default": {},
    "grounding-only": {"kind": "grounding"},
    "skills-only": {"kind": "skill"},
    "one-item": {"limit": 1},
}
FILES = (
    ("guide", GUIDE_PATH, None, ALL),
    ("unicode-path", UNICODE_PATH, None, ALL),
    ("binary", BINARY_PATH, None, ALL),
    ("empty", EMPTY_PATH, None, ALL),
    ("long-text", LONG_PATH, None, ALL),
    ("entry-point", "SKILL.md", None, ALL),
    ("missing", "docs/nope.md", None, NOBODY),
    ("directory", "docs", None, NOBODY),
    ("case-sensitive", "Docs/guide.md", None, NOBODY),
    ("climbing", "../../etc/passwd", "..%2F..%2Fetc%2Fpasswd", NOBODY),
    ("absolute", "/etc/passwd", "%2Fetc%2Fpasswd", NOBODY),
    ("backslash", "docs\\guide.md", "docs%5Cguide.md", NOBODY),
    ("nul", "docs\x00guide.md", "docs%00guide.md", NOBODY),
    ("dot-segment", "docs/./guide.md", "docs%2F.%2Fguide.md", NOBODY),
    ("empty-segment", "docs//guide.md", "docs%2F%2Fguide.md", NOBODY),
    ("too-long", "a" * 300, None, NOBODY),
)


def resolve(world: World, ref: str) -> str:
    return world.skill_id(ref.removeprefix("id:")) if ref.startswith("id:") else ref


def plain(path: str, params: Mapping[str, Any] | None = None) -> Fetch:
    async def fetch(rest: Rest, actor: str) -> tuple[Response, Any]:
        response = await rest.get(path, actor, params=params)
        return response, response.json() if response.status_code == 200 else None

    return fetch


def download_info(ref: str, version: str) -> Fetch:
    base = f"/skills/{ref}"

    async def fetch(rest: Rest, actor: str) -> tuple[Response, Any]:
        page = await rest.get(f"{base}/versions/{version}/download", actor)
        if page.status_code != 200:
            return page, None
        detail = (await rest.get(f"{base}/versions/{version}", actor)).json()
        entry = (await rest.get(base, actor)).json()
        info = {
            "name": entry["name"],
            "version": detail["version"],
            "sha256": detail["sha256"],
            "size_bytes": detail["size_bytes"],
            "url": detail["download_url"],
            "requires_auth": page.headers["cache-control"] == PRIVATE_CACHE_CONTROL,
        }
        return page, info

    return fetch


def search_read(name: str, arguments: Mapping[str, Any]) -> Read:
    params: dict[str, Any] = {
        "limit": arguments.get("limit", 10),
        "offset": arguments.get("offset", 0),
    }
    for key, parameter in (("query", "q"), ("kind", "kind"), ("category", "category")):
        if arguments.get(key):
            params[parameter] = arguments[key]
    if arguments.get("tags"):
        params["tag"] = list(arguments["tags"])
    return Read(f"search_skills/{name}", "search_skills", arguments, ALL, plain("/skills", params))


def taxonomy_read(tool: str, name: str, arguments: Mapping[str, Any]) -> Read:
    path = "/tags" if tool == "list_tags" else "/categories"
    params = {"limit": arguments.get("limit", 50)}
    if arguments.get("kind"):
        params["kind"] = arguments["kind"]
    return Read(f"{tool}/{name}", tool, arguments, ALL, plain(path, params))


def get_skill_read(world: World, ref: str, visible_to: frozenset[str]) -> Read:
    resolved = resolve(world, ref)
    return Read(
        f"get_skill/{ref}",
        "get_skill",
        {"name": resolved},
        visible_to,
        plain(f"/skills/{resolved}"),
    )


def version_read(world: World, target: Target, tool: str, suffix: str) -> Read:
    ref = resolve(world, target.ref)
    path = f"/skills/{ref}/versions/{target.version}{suffix}"
    return Read(
        f"{tool}/{target.ref}@{target.version}",
        tool,
        {"name": ref, "version": target.version},
        target.visible_to,
        plain(path),
    )


def entrypoint_read(world: World, target: Target) -> Read:
    return version_read(world, target, "get_entrypoint", "/entrypoint")


def files_read(world: World, target: Target) -> Read:
    return version_read(world, target, "list_files", "/files")


def file_read(
    world: World,
    target: Target,
    path: str,
    visible_to: frozenset[str],
    encoded: str | None = None,
) -> Read:
    ref = resolve(world, target.ref)
    rest_path = encoded if encoded is not None else quote(path, safe="/")
    return Read(
        f"read_file/{target.ref}@{target.version}/{path[:40]!r}",
        "read_file",
        {"name": ref, "version": target.version, "path": path},
        visible_to,
        plain(f"/skills/{ref}/versions/{target.version}/files/{rest_path}"),
    )


def download_read(world: World, target: Target) -> Read:
    ref = resolve(world, target.ref)
    return Read(
        f"get_download_info/{target.ref}@{target.version}",
        "get_download_info",
        {"name": ref, "version": target.version},
        target.visible_to,
        download_info(ref, target.version),
    )


def search_reads() -> list[Read]:
    return [search_read(name, arguments) for name, arguments in SEARCHES.items()]


def taxonomy_reads() -> list[Read]:
    return [
        taxonomy_read(tool, name, arguments)
        for tool in ("list_tags", "list_categories")
        for name, arguments in TAXONOMIES.items()
    ]


def file_reads(world: World) -> list[Read]:
    base = TARGETS[0]
    reads = [file_read(world, base, path, visible, encoded) for _, path, encoded, visible in FILES]
    for target in TARGETS[1:]:
        reads.append(file_read(world, target, target.file, target.visible_to))
    return reads


def compare_result(
    read: Read, actor: str, response: Response, expected: Any, result: Any
) -> list[str]:
    label = f"{read.id} as {actor}"
    if len(result.content) != 1 or result.content[0].type != "text":
        return [f"{label}: expected one text block, got {result.content!r}"]
    text = result.content[0].text
    if response.status_code == 200:
        if result.is_error:
            return [f"{label}: REST answered 200 but MCP failed with {text!r}"]
        payload = json.loads(text)
        problems = [f"{label}: {item}" for item in differences(expected, payload)]
        wanted = {"result": payload} if isinstance(payload, list) else payload
        if result.structured_content != wanted:
            problems.append(f"{label}: the structured content is not the JSON of the text block")
        return problems
    error = response.json()["error"]
    if not result.is_error:
        return [f"{label}: REST answered {error['code']} but MCP succeeded"]
    if text != describe(error):
        return [f"{label}: REST says {describe(error)!r} but MCP says {text!r}"]
    return []


async def run_reads(reads: list[Read], world: World, rest: Rest, connect: McpConnector) -> Report:
    report = Report(successes={read.id: set() for read in reads})
    for actor in ACTOR_NAMES:
        async with connect(world.credentials[actor]) as session:
            for read in reads:
                response, expected = await read.fetch(rest, actor)
                result = await session.call_tool(read.tool, dict(read.arguments))
                report.problems += compare_result(read, actor, response, expected, result)
                if response.status_code == 200:
                    report.successes[read.id].add(actor)
    return report


async def assert_reads_match(
    reads: list[Read], world: World, rest: Rest, connect: McpConnector
) -> None:
    report = await run_reads(reads, world, rest, connect)
    assert report.problems == []
    assert {read.id: report.successes[read.id] for read in reads} == {
        read.id: set(read.visible_to) for read in reads
    }


class TestTheSeededRegistry:
    async def test_there_is_enough_in_the_registry_for_the_comparisons_to_mean_something(
        self, rest
    ):
        everyone = (await rest.get("/skills", "owner", params={"limit": 50})).json()
        public = (await rest.get("/skills", "anonymous", params={"limit": 50})).json()
        versions = (await rest.get(f"/skills/{PUBLISHED}/versions", "owner")).json()
        files = (await rest.get(f"/skills/{PUBLISHED}/versions/latest/files")).json()

        assert {item["name"]: item["status"] for item in everyone["items"]} == {
            API_REFERENCE: "published",
            DRAFT: "draft",
            FOREIGN: "published",
            HIDDEN: "unpublished",
            PUBLISHED: "published",
        }
        assert [item["name"] for item in public["items"]] == [API_REFERENCE, FOREIGN, PUBLISHED]
        assert [(item["version"], item["status"]) for item in versions] == [
            ("2.0.0", "draft"),
            ("2.0.0-beta.1", "published"),
            ("1.1.0", "published"),
            ("1.0.0", "published"),
            ("0.9.0", "deprecated"),
        ]
        assert files["version"] == "1.1.0"
        assert {item["path"]: item["text"] for item in files["files"]}[BINARY_PATH] is False
        assert len(files["files"]) == 7

    async def test_every_actor_has_a_working_credential(self, rest, mcp):
        for actor in ("owner", "owner_read", "owner_jwt", "stranger", "admin", "admin_read"):
            assert (await rest.get("/auth/me", actor)).status_code == 200
            assert (await mcp.call("list_tags", {}, actor)).ok
        assert (await rest.get("/auth/me", "anonymous")).status_code == 401
        for actor in ("invalid_token", "invalid_jwt"):
            assert (await rest.get("/auth/me", actor)).status_code == 401
            assert (await mcp.call("list_tags", {}, actor)).status == 401


class TestReadsReturnTheSameJson:
    async def test_search_skills_is_the_rest_listing(self, world, rest, mcp_connect):
        await assert_reads_match(search_reads(), world, rest, mcp_connect)

    async def test_get_skill_is_the_rest_entry_detail(self, world, rest, mcp_connect):
        reads = [get_skill_read(world, ref, visible_to) for ref, visible_to in ENTRIES]

        await assert_reads_match(reads, world, rest, mcp_connect)

    async def test_get_entrypoint_is_the_rest_entrypoint(self, world, rest, mcp_connect):
        reads = [entrypoint_read(world, target) for target in TARGETS]

        await assert_reads_match(reads, world, rest, mcp_connect)

    async def test_list_files_is_the_rest_manifest(self, world, rest, mcp_connect):
        reads = [files_read(world, target) for target in TARGETS]

        await assert_reads_match(reads, world, rest, mcp_connect)

    async def test_read_file_is_the_rest_file_content(self, world, rest, mcp_connect):
        await assert_reads_match(file_reads(world), world, rest, mcp_connect)

    async def test_list_tags_and_list_categories_are_the_rest_taxonomies(
        self, world, rest, mcp_connect
    ):
        await assert_reads_match(taxonomy_reads(), world, rest, mcp_connect)

    async def test_get_download_info_is_what_rest_says_about_the_version_and_its_download(
        self, world, rest, mcp_connect
    ):
        reads = [download_read(world, target) for target in TARGETS]

        await assert_reads_match(reads, world, rest, mcp_connect)

    @pytest.mark.parametrize("limit", [1, 15, 16, 17, 100])
    async def test_inline_reads_are_cut_at_the_same_character_boundary(
        self, world, rest, mcp_connect, monkeypatch, limit
    ):
        monkeypatch.setattr(settings.registry.limits, "max_inline_read_bytes", limit)
        base = TARGETS[0]
        reads = [
            file_read(world, base, LONG_PATH, ALL),
            file_read(world, base, UNICODE_PATH, ALL),
            file_read(world, base, GUIDE_PATH, ALL),
            entrypoint_read(world, base),
        ]

        await assert_reads_match(reads, world, rest, mcp_connect)

        async with mcp_connect() as session:
            result = await session.call_tool("read_file", {"name": PUBLISHED, "path": UNICODE_PATH})
        payload = json.loads(result.content[0].text)
        assert payload["truncated"] is (limit < len(UNICODE_TEXT.encode("utf-8")))
        assert len(payload["content"].encode("utf-8")) <= limit
        assert UNICODE_TEXT.startswith(payload["content"])

    async def test_a_uuid_reference_resolves_to_the_same_entry_over_both_interfaces(
        self, world, rest, mcp_connect
    ):
        entry_id = world.skill_id(PUBLISHED)

        async with mcp_connect() as session:
            result = await session.call_tool("get_entrypoint", {"name": entry_id})
        over_rest = (await rest.get(f"/skills/{entry_id}/versions/latest/entrypoint")).json()

        assert json.loads(result.content[0].text) == over_rest
        assert over_rest["name"] == PUBLISHED


class TestTheDownloadInformationLeadsToTheSamePackage:
    @pytest.mark.parametrize("actor", ["anonymous", "stranger", "owner"])
    async def test_the_url_and_hash_from_mcp_fetch_the_zip_that_was_uploaded(
        self, world, rest, mcp, actor
    ):
        info = (await mcp.call("get_download_info", {"name": PUBLISHED}, actor)).payload

        page = await rest.client.get(path_of_url(info["url"]), headers=headers_for(world, actor))

        original = world.zip_of(PUBLISHED, "1.1.0")
        assert info["url"].startswith("https://registry.example.test/api/v1/skills/")
        assert info["version"] == "1.1.0"
        assert info["sha256"] == hashlib.sha256(original).hexdigest()
        assert info["size_bytes"] == len(original)
        assert page.status_code == 200
        assert page.content == original
        assert page.headers["x-content-sha256"] == info["sha256"]
        assert info["requires_auth"] is False

    async def test_a_private_version_says_it_needs_a_credential_and_rest_agrees(
        self, world, rest, mcp
    ):
        info = await mcp.call("get_download_info", {"name": DRAFT, "version": "0.1.0"}, "owner")
        anonymous = await rest.client.get(path_of_url(info.payload["url"]))
        owner = await rest.client.get(
            path_of_url(info.payload["url"]), headers=headers_for(world, "owner")
        )

        assert info.payload["requires_auth"] is True
        assert anonymous.status_code == 404
        assert owner.status_code == 200
        assert owner.headers["cache-control"] == PRIVATE_CACHE_CONTROL
        assert hashlib.sha256(owner.content).hexdigest() == info.payload["sha256"]

    async def test_with_private_reading_every_download_needs_a_credential(
        self, world, rest, mcp, monkeypatch
    ):
        monkeypatch.setattr(settings.registry, "public_read", False)

        info = (await mcp.call("get_download_info", {"name": PUBLISHED}, "stranger")).payload
        anonymous = await rest.client.get(path_of_url(info["url"]))
        stranger = await rest.client.get(
            path_of_url(info["url"]), headers=headers_for(world, "stranger")
        )

        assert info["requires_auth"] is True
        assert anonymous.status_code == 401
        assert stranger.status_code == 200
        assert stranger.headers["cache-control"] == PRIVATE_CACHE_CONTROL


def headers_for(world: World, actor: str) -> dict[str, str]:
    credential = world.credentials[actor]
    return {"Authorization": f"Bearer {credential}"} if credential else {}


RestCall = Callable[[World, Rest], Awaitable[Response]]
McpCall = Callable[[World, Mcp], Awaitable[ToolOutcome]]


@dataclass(frozen=True)
class Failure:
    id: str
    code: str
    rest: RestCall
    mcp: McpCall
    reason: str | None = None
    configure: Callable[[pytest.MonkeyPatch], None] | None = None


def rest_get(path: str, actor: str) -> RestCall:
    async def call(world: World, rest: Rest) -> Response:
        return await rest.get(path, actor)

    return call


def rest_post(path: str, actor: str) -> RestCall:
    async def call(world: World, rest: Rest) -> Response:
        return await rest.post(path, actor)

    return call


def rest_upload(
    build: Callable[[World], bytes],
    actor: str,
    *,
    version: str | None = None,
    publish: bool | None = None,
) -> RestCall:
    async def call(world: World, rest: Rest) -> Response:
        return await rest.upload(build(world), actor, version=version, publish=publish)

    return call


def mcp_tool(tool: str, arguments: Mapping[str, Any], actor: str) -> McpCall:
    async def call(world: World, mcp: Mcp) -> ToolOutcome:
        return await mcp.call(tool, arguments, actor)

    return call


def mcp_upload(
    build: Callable[[World], bytes],
    actor: str,
    *,
    version: str | None = None,
    publish: bool = False,
) -> McpCall:
    async def call(world: World, mcp: Mcp) -> ToolOutcome:
        return await mcp.upload(build(world), actor, version=version, publish=publish)

    return call


def read_failure(
    id: str,
    actor: str,
    tool: str,
    arguments: Mapping[str, Any],
    path: str,
    code: str,
    reason: str | None = None,
) -> Failure:
    return Failure(id, code, rest_get(path, actor), mcp_tool(tool, arguments, actor), reason=reason)


def upload_failure(
    id: str,
    actor: str,
    build: Callable[[World], bytes],
    code: str,
    *,
    version: str | None = None,
    reason: str | None = None,
    configure: Callable[[pytest.MonkeyPatch], None] | None = None,
) -> Failure:
    return Failure(
        id,
        code,
        rest_upload(build, actor, version=version),
        mcp_upload(build, actor, version=version),
        reason=reason,
        configure=configure,
    )


def manage_failure(id: str, actor: str, action: str, name: str, version: str, code: str) -> Failure:
    return Failure(
        id,
        code,
        rest_post(f"/skills/{name}/versions/{version}/{action}", actor),
        mcp_tool(f"{action}_version", {"name": name, "version": version}, actor),
    )


def valid_skill(name: str = "fresh-skill", version: str = "1.0.0") -> Callable[[World], bytes]:
    return lambda world: make_skill_zip(name, version)


def limit_to(size: int) -> Callable[[pytest.MonkeyPatch], None]:
    def configure(monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings.registry.limits, "max_package_bytes", size)

    return configure


def oversized(world: World) -> bytes:
    noise = random.Random(7).randbytes(120_000)
    return make_skill_zip("too-big", "1.0.0", files={"noise.bin": noise})


def unsafe(world: World) -> bytes:
    return make_zip({"SKILL.md": skill_md("unsafe-skill", "1.0.0"), "../escape.txt": "outside"})


FAILURES: list[Failure] = [
    read_failure(
        "get_skill/unknown",
        "owner",
        "get_skill",
        {"name": MISSING},
        f"/skills/{MISSING}",
        "NOT_FOUND",
    ),
    read_failure(
        "get_skill/draft-of-another-user",
        "stranger",
        "get_skill",
        {"name": DRAFT},
        f"/skills/{DRAFT}",
        "NOT_FOUND",
    ),
    read_failure(
        "get_skill/hidden-for-anonymous",
        "anonymous",
        "get_skill",
        {"name": HIDDEN},
        f"/skills/{HIDDEN}",
        "NOT_FOUND",
    ),
    read_failure(
        "get_entrypoint/unknown-version",
        "anonymous",
        "get_entrypoint",
        {"name": PUBLISHED, "version": "9.9.9"},
        f"/skills/{PUBLISHED}/versions/9.9.9/entrypoint",
        "NOT_FOUND",
    ),
    read_failure(
        "get_entrypoint/draft-version-of-another-user",
        "stranger",
        "get_entrypoint",
        {"name": PUBLISHED, "version": "2.0.0"},
        f"/skills/{PUBLISHED}/versions/2.0.0/entrypoint",
        "NOT_FOUND",
    ),
    read_failure(
        "get_entrypoint/nothing-published",
        "owner",
        "get_entrypoint",
        {"name": DRAFT},
        f"/skills/{DRAFT}/versions/latest/entrypoint",
        "NOT_FOUND",
    ),
    read_failure(
        "list_files/unknown",
        "owner",
        "list_files",
        {"name": MISSING},
        f"/skills/{MISSING}/versions/latest/files",
        "NOT_FOUND",
    ),
    read_failure(
        "read_file/not-in-the-package",
        "anonymous",
        "read_file",
        {"name": PUBLISHED, "path": "docs/nope.md"},
        f"/skills/{PUBLISHED}/versions/latest/files/docs/nope.md",
        "NOT_FOUND",
    ),
    *[
        read_failure(
            f"read_file/invalid-path/{encoded[:24]}",
            "anonymous",
            "read_file",
            {"name": PUBLISHED, "path": unquote(encoded)},
            f"/skills/{PUBLISHED}/versions/latest/files/{encoded}",
            "VALIDATION_ERROR",
            reason="INVALID_PATH",
        )
        for encoded in (
            "..%2F..%2Fetc%2Fpasswd",
            "%2Fetc%2Fpasswd",
            "docs%5Cguide.md",
            "docs%00guide.md",
            "docs%2F..%2Fguide.md",
            "a" * 300,
        )
    ],
    read_failure(
        "get_download_info/unknown",
        "anonymous",
        "get_download_info",
        {"name": MISSING},
        f"/skills/{MISSING}/versions/latest/download",
        "NOT_FOUND",
    ),
    upload_failure("upload/anonymous", "anonymous", valid_skill(), "UNAUTHORIZED"),
    upload_failure("upload/read-only-token", "owner_read", valid_skill(), "FORBIDDEN"),
    upload_failure("upload/admin-read-only-token", "admin_read", valid_skill(), "FORBIDDEN"),
    upload_failure(
        "upload/not-a-zip",
        "owner",
        lambda world: b"this is not a zip",
        "INVALID_PACKAGE",
        reason="NOT_A_ZIP",
    ),
    upload_failure(
        "upload/no-entry-point",
        "owner",
        lambda world: make_zip({"readme.md": "no entry point"}),
        "INVALID_PACKAGE",
        reason="MISSING_ENTRYPOINT",
    ),
    upload_failure("upload/unsafe-path", "owner", unsafe, "INVALID_PACKAGE", reason="UNSAFE_PATH"),
    upload_failure(
        "upload/no-frontmatter",
        "owner",
        lambda world: make_zip({"SKILL.md": "# no metadata\n"}),
        "INVALID_PACKAGE",
        reason="BAD_FRONTMATTER",
    ),
    upload_failure(
        "upload/no-version-anywhere",
        "owner",
        lambda world: make_skill_zip("versionless", None),
        "INVALID_PACKAGE",
        reason="MISSING_FIELD",
    ),
    upload_failure(
        "upload/version-mismatch",
        "owner",
        valid_skill("mismatch-skill", "1.0.0"),
        "INVALID_PACKAGE",
        version="2.0.0",
        reason="VERSION_MISMATCH",
    ),
    upload_failure(
        "upload/duplicate-version",
        "owner",
        lambda world: world.zip_of(PUBLISHED, "1.0.0"),
        "CONFLICT",
    ),
    upload_failure(
        "upload/kind-change",
        "owner",
        lambda world: make_grounding_zip(PUBLISHED, "5.0.0"),
        "CONFLICT",
    ),
    upload_failure(
        "upload/entry-of-another-user",
        "stranger",
        lambda world: make_skill_zip(PUBLISHED, "6.0.0"),
        "FORBIDDEN",
    ),
    upload_failure(
        "upload/over-the-size-limit",
        "owner",
        oversized,
        "PAYLOAD_TOO_LARGE",
        configure=limit_to(100_000),
    ),
    manage_failure("publish/unknown-entry", "owner", "publish", MISSING, "1.0.0", "NOT_FOUND"),
    manage_failure("publish/unknown-version", "owner", "publish", PUBLISHED, "9.9.9", "NOT_FOUND"),
    manage_failure(
        "publish/latest-is-not-a-version", "owner", "publish", PUBLISHED, "latest", "NOT_FOUND"
    ),
    manage_failure(
        "publish/entry-of-another-user", "stranger", "publish", PUBLISHED, "2.0.0", "FORBIDDEN"
    ),
    manage_failure(
        "publish/draft-entry-of-another-user", "stranger", "publish", DRAFT, "0.1.0", "NOT_FOUND"
    ),
    manage_failure("publish/anonymous", "anonymous", "publish", PUBLISHED, "2.0.0", "UNAUTHORIZED"),
    manage_failure(
        "publish/read-only-token", "owner_read", "publish", PUBLISHED, "2.0.0", "FORBIDDEN"
    ),
    manage_failure(
        "deprecate/the-only-published-version",
        "stranger",
        "deprecate",
        FOREIGN,
        "1.0.0",
        "CONFLICT",
    ),
    manage_failure(
        "deprecate/a-draft-version", "owner", "deprecate", PUBLISHED, "2.0.0", "CONFLICT"
    ),
    manage_failure(
        "deprecate/entry-of-another-user",
        "stranger",
        "deprecate",
        PUBLISHED,
        "1.0.0",
        "FORBIDDEN",
    ),
    manage_failure(
        "deprecate/unknown-version", "admin", "deprecate", PUBLISHED, "9.9.9", "NOT_FOUND"
    ),
]


class TestDomainErrorsAreTheSameOverBothInterfaces:
    @pytest.mark.parametrize("failure", FAILURES, ids=lambda failure: failure.id)
    async def test_the_tool_error_text_is_code_message_and_reason_of_the_rest_error(
        self, world, rest, mcp, monkeypatch, failure
    ):
        if failure.configure is not None:
            failure.configure(monkeypatch)

        response = await failure.rest(world, rest)
        result = await failure.mcp(world, mcp)

        error = response.json()["error"]
        assert response.status_code == STATUS_OF_CODE[failure.code]
        assert error["code"] == failure.code
        assert error.get("reason") == failure.reason
        assert result.is_error is True
        assert result.text == describe(error)
        assert (result.code, result.reason) == (failure.code, failure.reason)
        assert result.message == error["message"]

    async def test_a_damaged_package_is_a_storage_error_over_both_without_internal_details(
        self, world, rest, mcp, blob_store
    ):
        sha256 = hashlib.sha256(world.zip_of(PUBLISHED, "1.1.0")).hexdigest()
        (blob_store.root / sha256[:2] / f"{sha256}.zip").unlink()

        response = await rest.get(f"/skills/{PUBLISHED}/versions/1.1.0/entrypoint")
        result = await mcp.call("get_entrypoint", {"name": PUBLISHED, "version": "1.1.0"})

        assert response.status_code == 500
        assert response.json()["error"]["code"] == "STORAGE_ERROR"
        assert result.is_error is True
        assert result.code == "STORAGE_ERROR"
        for text in (response.text, result.text):
            assert str(blob_store.root) not in text
            assert sha256 not in text
            assert "Traceback" not in text


class TestCredentialErrorsAreTheSameOverBothInterfaces:
    @pytest.mark.parametrize(
        "header",
        [
            "Bearer alx_" + "z" * 43,
            "Bearer not-a-jwt",
            "Basic dXNlcjpwYXNz",
            "Bearer",
            "Bearer two words",
            "bearer",
            "",
        ],
        ids=[
            "unknown-token",
            "unknown-jwt",
            "basic",
            "no-credential",
            "two-words",
            "lower",
            "empty",
        ],
    )
    async def test_a_bad_authorization_header_is_the_same_401_body(self, rest, mcp, header):
        over_rest = await rest.get("/skills", "anonymous", headers={"Authorization": header})
        over_mcp = await mcp.post(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            "anonymous",
            headers={"Authorization": header},
        )

        assert over_rest.status_code == over_mcp.status_code == 401
        assert over_rest.json() == over_mcp.json()
        assert over_rest.headers["www-authenticate"] == over_mcp.headers["www-authenticate"]
        assert over_rest.headers["www-authenticate"] == "Bearer"

    async def test_two_authorization_headers_are_refused_by_both(self, world, rest, mcp):
        headers = [
            ("authorization", f"Bearer {world.credentials['owner']}"),
            ("authorization", f"Bearer {world.credentials['admin']}"),
        ]

        over_rest = await rest.client.get("/api/v1/skills", headers=headers)
        over_mcp = await rest.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers=[*headers, ("accept", "application/json, text/event-stream")],
        )

        assert over_rest.status_code == over_mcp.status_code == 401
        assert over_rest.json() == over_mcp.json()

    async def test_private_reading_refuses_the_anonymous_with_the_same_body(
        self, rest, mcp, monkeypatch
    ):
        monkeypatch.setattr(settings.registry, "public_read", False)

        over_rest = await rest.get("/skills")
        over_mcp = await mcp.post({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})

        assert over_rest.status_code == over_mcp.status_code == 401
        assert over_rest.json() == over_mcp.json()
        assert over_rest.json()["error"]["code"] == "UNAUTHORIZED"


class TestCredentialLifecycleIsTheSameOverBothInterfaces:
    async def assert_refused_everywhere(self, rest: Rest, mcp: Mcp, actor: str) -> None:
        over_rest = await rest.get("/auth/me", actor)
        listing = await rest.get("/skills", actor)
        over_mcp = await mcp.call("list_tags", {}, actor)
        publish = await mcp.call("publish_version", {"name": DRAFT, "version": "0.1.0"}, actor)

        error = over_rest.json()["error"]
        assert over_rest.status_code == listing.status_code == 401
        assert over_mcp.status == publish.status == 401
        assert over_rest.headers["www-authenticate"] == "Bearer"
        assert listing.json() == over_rest.json()
        for result in (over_mcp, publish):
            assert (result.code, result.message) == (error["code"], error["message"])

    async def test_a_deactivated_user_loses_its_api_tokens_and_its_jwt(self, world, rest, mcp):
        stranger = world.principal("stranger")
        world.credentials["stranger_jwt"] = await issue_jwt(world.factory, stranger)
        assert (await rest.get("/auth/me", "stranger_jwt")).status_code == 200
        async with world.factory() as db:
            user = await user_repo.get_user(db, stranger.user_id)
            assert user is not None
            await user_repo.deactivate_user(db, user)
            await db.commit()

        for actor in ("stranger", "stranger_read", "stranger_jwt"):
            await self.assert_refused_everywhere(rest, mcp, actor)
        assert (await rest.get("/auth/me", "owner")).status_code == 200
        assert (await mcp.call("list_tags", {}, "owner")).ok

    async def test_a_revoked_token_is_refused_while_its_siblings_keep_working(
        self, world, rest, mcp
    ):
        owner = world.principal("owner")
        async with world.factory() as db:
            service = TokenService(db)
            tokens = {token.name: token for token in await service.list(owner)}
            await service.revoke(owner, tokens["owner"].id)

        await self.assert_refused_everywhere(rest, mcp, "owner")
        for actor in ("owner_read", "owner_jwt"):
            assert (await rest.get("/auth/me", actor)).status_code == 200
            assert (await mcp.call("list_tags", {}, actor)).ok

    async def test_an_expired_token_is_refused(self, world, rest, mcp):
        async with world.factory() as db:
            await db.execute(
                update(ApiToken)
                .where(ApiToken.name == "admin")
                .values(expires_at=datetime.now(UTC) - timedelta(days=1))
            )
            await db.commit()

        await self.assert_refused_everywhere(rest, mcp, "admin")
        assert (await rest.get("/auth/me", "admin_read")).status_code == 200


def normalized(result: Mapping[str, Any]) -> dict[str, Any]:
    copy = json.loads(json.dumps(result))
    for section in ("skill", "version"):
        for key in ("id", "skill_id", "created_at", "updated_at", "published_at", "deprecated_at"):
            if copy[section].get(key) is not None:
                copy[section][key] = f"<{key}>"
    return copy


@dataclass(frozen=True)
class Upload:
    id: str
    build: Callable[[World], bytes]
    actor: str = "owner"
    version: str | None = None
    publish: bool | None = None
    auto_publish: bool = False
    owner: str = "owner"


UPLOADS = (
    Upload(
        "published-skill-with-metadata",
        lambda world: make_skill_zip(
            "parity-skill",
            "1.0.0",
            tags=["Alpha", "beta", "alpha"],
            category="Docs",
            display_name="Parity Skill",
            description="Checks that both interfaces agree.",
            extra_frontmatter={"license": "MIT", "extra": {"nested": [1, 2]}},
            files={"docs/a.md": "alpha\n", "bin/x.bin": bytes(range(40))},
        ),
        publish=True,
    ),
    Upload("draft-skill", lambda world: make_skill_zip("parity-draft", "0.1.0")),
    Upload(
        "grounding-in-a-wrapper-directory-with-junk",
        lambda world: make_grounding_zip(
            "parity-ground",
            "2.3.4-rc.1",
            wrapper="parity-ground",
            macos_junk=True,
            dir_entries=True,
            files={"tables/a.csv": "a,b\n1,2\n"},
        ),
        publish=True,
    ),
    Upload(
        "version-given-as-an-argument",
        lambda world: make_skill_zip("parity-late", None),
        version="3.1.4",
        publish=True,
    ),
    Upload(
        "version-given-twice-and-equal",
        lambda world: make_skill_zip("parity-twice", "1.2.3"),
        version="1.2.3",
    ),
    Upload(
        "registry-publishes-by-default",
        lambda world: make_skill_zip("parity-auto", "1.0.0"),
        auto_publish=True,
    ),
    Upload(
        "new-version-of-an-existing-entry",
        lambda world: make_skill_zip(PUBLISHED, "3.0.0", description="Third generation"),
        publish=True,
    ),
    Upload(
        "an-admin-uploads-to-someone-elses-entry",
        lambda world: make_skill_zip(FOREIGN, "1.1.0", tags=["new"]),
        actor="admin",
        publish=True,
        owner="stranger",
    ),
    Upload(
        "a-hidden-entry-stays-hidden",
        lambda world: make_grounding_zip(HIDDEN, "1.1.0"),
        publish=True,
    ),
    Upload(
        "unicode-everywhere",
        lambda world: make_skill_zip(
            "parity-unicode",
            "1.0.0",
            description="Árbol, ñandú y 日本語",
            display_name="Ünïcödé",
            files={"docs/ñandú.md": "contenido\n"},
        ),
        publish=True,
    ),
)


class TestUploadsProduceTheSameResult:
    @pytest.mark.parametrize("case", UPLOADS, ids=lambda case: case.id)
    async def test_rest_and_mcp_store_the_same_entry_and_version(
        self, world, rest, mcp, monkeypatch, case
    ):
        monkeypatch.setattr(settings.registry, "auto_publish", case.auto_publish)
        data = case.build(world)

        over_rest = await rest.upload(data, case.actor, version=case.version, publish=case.publish)
        await world.reset()
        over_mcp = await mcp.upload(
            data, case.actor, version=case.version, publish=bool(case.publish)
        )

        assert over_rest.status_code == 201, over_rest.text
        assert over_mcp.ok, over_mcp.text
        rest_result, mcp_result = over_rest.json(), over_mcp.payload
        assert differences(normalized(rest_result), normalized(mcp_result)) == []
        assert rest_result["skill"]["owner_id"] == world.principal(case.owner).user_id
        for result in (rest_result, mcp_result):
            assert result["version"]["skill_id"] == result["skill"]["id"]
            assert len(uuid.UUID(result["skill"]["id"]).hex) == 32
            assert len(uuid.UUID(result["version"]["id"]).hex) == 32
            assert result["version"]["sha256"] == hashlib.sha256(data).hexdigest()
            assert result["version"]["size_bytes"] == len(data)
            assert result["version"]["uploaded_by"] == world.principal(case.actor).user_id

    @pytest.mark.parametrize("case", UPLOADS[:3], ids=lambda case: case.id)
    async def test_what_each_interface_stored_reads_back_identically_through_the_other(
        self, world, rest, mcp, mcp_connect, case
    ):
        data = case.build(world)
        over_rest = await rest.upload(data, case.actor, version=case.version, publish=case.publish)
        name = over_rest.json()["skill"]["name"]

        async with mcp_connect(world.credentials[case.actor]) as session:
            detail = await session.call_tool("get_skill", {"name": name})
            files = await session.call_tool("list_files", {"name": name})
        over_rest_detail = (await rest.get(f"/skills/{name}", case.actor)).json()
        over_rest_files = await rest.get(f"/skills/{name}/versions/latest/files", case.actor)

        assert json.loads(detail.content[0].text) == over_rest_detail
        if over_rest.json()["version"]["status"] == "published":
            assert json.loads(files.content[0].text) == over_rest_files.json()

    async def test_publish_false_over_mcp_follows_the_registry_default_unlike_rest(
        self, world, rest, mcp, monkeypatch
    ):
        monkeypatch.setattr(settings.registry, "auto_publish", True)
        data = make_skill_zip("explicit-draft", "1.0.0")

        over_rest = await rest.upload(data, "owner", publish=False)
        await world.reset()
        over_mcp = await mcp.upload(data, "owner", publish=False)

        assert over_rest.json()["version"]["status"] == "draft"
        assert over_mcp.payload["version"]["status"] == "published"


def normalized_version(summary: Mapping[str, Any]) -> dict[str, Any]:
    copy = json.loads(json.dumps(summary))
    for key in ("published_at", "deprecated_at"):
        if copy[key] is not None:
            copy[key] = f"<{key}>"
    return copy


STATE_CHANGES = (
    ("publish", DRAFT, "0.1.0", "published"),
    ("publish", PUBLISHED, "2.0.0", "published"),
    ("publish", PUBLISHED, "1.0.0", "published"),
    ("publish", PUBLISHED, "0.9.0", "published"),
    ("deprecate", PUBLISHED, "1.0.0", "deprecated"),
    ("deprecate", PUBLISHED, "0.9.0", "deprecated"),
)


class TestStateChangesReturnTheSameVersionSummary:
    @pytest.mark.parametrize(
        ("action", "name", "version", "status"),
        STATE_CHANGES,
        ids=lambda value: str(value),
    )
    async def test_publishing_and_deprecating_answer_with_the_same_summary(
        self, world, rest, mcp, action, name, version, status
    ):
        over_rest = await rest.post(f"/skills/{name}/versions/{version}/{action}", "owner")
        await world.reset()
        over_mcp = await mcp.call(f"{action}_version", {"name": name, "version": version}, "owner")

        assert over_rest.status_code == 200, over_rest.text
        assert over_mcp.ok, over_mcp.text
        assert (
            differences(normalized_version(over_rest.json()), normalized_version(over_mcp.payload))
            == []
        )
        assert over_mcp.payload["status"] == status
        assert over_mcp.payload["version"] == version

    async def test_what_each_interface_changed_is_visible_through_the_other(self, world, rest, mcp):
        await rest.post(f"/skills/{DRAFT}/versions/0.1.0/publish", "owner")
        await mcp.call("deprecate_version", {"name": PUBLISHED, "version": "1.0.0"}, "owner")

        over_mcp = await mcp.call("get_skill", {"name": DRAFT}, "anonymous")
        over_rest = await rest.get(f"/skills/{PUBLISHED}/versions", "anonymous")

        assert over_mcp.payload["status"] == "published"
        assert over_mcp.payload["latest_version"] == "0.1.0"
        statuses = {item["version"]: item["status"] for item in over_rest.json()}
        assert statuses["1.0.0"] == "deprecated"


MATRIX_TARGETS = tuple(
    target
    for target in TARGETS
    if (target.ref, target.version)
    in {
        (PUBLISHED, "latest"),
        (PUBLISHED, "0.9.0"),
        (PUBLISHED, "2.0.0"),
        (PUBLISHED, "9.9.9"),
        (DRAFT, "latest"),
        (HIDDEN, "1.0.0"),
        (FOREIGN, "1.0.0"),
        (MISSING, "latest"),
    }
)
MATRIX_ENTRIES = tuple((ref, visible_to) for ref, visible_to in ENTRIES if ref != f"id:{DRAFT}")
MATRIX_SEARCHES = ("everything", "one-word", "combined")
Build = Callable[[World], Read]


def read_specs() -> list[Any]:
    specs: list[tuple[str, Build]] = []
    for name in MATRIX_SEARCHES:
        specs.append(
            (f"search_skills/{name}", lambda world, name=name: search_read(name, SEARCHES[name]))
        )
    for tool in ("list_tags", "list_categories"):
        specs.append(
            (f"{tool}/default", lambda world, tool=tool: taxonomy_read(tool, "default", {}))
        )
    for ref, visible_to in MATRIX_ENTRIES:
        specs.append(
            (
                f"get_skill/{ref}",
                lambda world, ref=ref, visible_to=visible_to: get_skill_read(
                    world, ref, visible_to
                ),
            )
        )
    for target in MATRIX_TARGETS:
        label = f"{target.ref}@{target.version}"
        specs.append(
            (f"get_entrypoint/{label}", lambda world, target=target: entrypoint_read(world, target))
        )
        specs.append(
            (f"list_files/{label}", lambda world, target=target: files_read(world, target))
        )
        specs.append(
            (
                f"read_file/{label}",
                lambda world, target=target: file_read(
                    world, target, target.file, target.visible_to
                ),
            )
        )
        specs.append(
            (
                f"get_download_info/{label}",
                lambda world, target=target: download_read(world, target),
            )
        )
    return [pytest.param(build, id=spec_id) for spec_id, build in specs]


@dataclass(frozen=True)
class Write:
    id: str
    rest: Callable[[World, Rest, str], Awaitable[Response]]
    mcp: Callable[[World, Mcp, str], Awaitable[ToolOutcome]]
    expected: Callable[[str], str | None]


def read_code(actor: str, visible_to: frozenset[str], public_read: bool) -> str | None:
    if actor.startswith("invalid"):
        return "UNAUTHORIZED"
    actor = "owner" if actor == "owner_jwt" else actor
    if actor == "anonymous" and not public_read:
        return "UNAUTHORIZED"
    return None if actor in visible_to else "NOT_FOUND"


def gate_code(actor: str) -> str | None:
    if actor.startswith("invalid") or actor == "anonymous":
        return "UNAUTHORIZED"
    if actor.endswith("_read"):
        return "FORBIDDEN"
    return None


def manage_code(actor: str, entry: str, otherwise: str | None) -> str | None:
    gate = gate_code(actor)
    if gate is not None:
        return gate
    if entry == MISSING:
        return "NOT_FOUND"
    outcome = write_outcome("owner" if actor == "owner_jwt" else actor, entry)
    return otherwise if outcome is None else ERROR_CODES[outcome]


def upload_code(
    actor: str, allowed: frozenset[str], allowed_result: str | None, denied_result: str | None
) -> str | None:
    gate = gate_code(actor)
    if gate is not None:
        return gate
    return (
        allowed_result if ("owner" if actor == "owner_jwt" else actor) in allowed else denied_result
    )


def manage_write(action: str, name: str, version: str, otherwise: str | None) -> Write:
    async def over_rest(world: World, rest: Rest, actor: str) -> Response:
        return await rest.post(f"/skills/{name}/versions/{version}/{action}", actor)

    async def over_mcp(world: World, mcp: Mcp, actor: str) -> ToolOutcome:
        return await mcp.call(f"{action}_version", {"name": name, "version": version}, actor)

    return Write(
        f"{action}/{name}@{version}",
        over_rest,
        over_mcp,
        lambda actor: manage_code(actor, name, otherwise),
    )


def upload_write(
    id: str,
    build: Callable[[World], bytes],
    allowed: frozenset[str],
    allowed_result: str | None = None,
    denied_result: str | None = "FORBIDDEN",
) -> Write:
    async def over_rest(world: World, rest: Rest, actor: str) -> Response:
        return await rest.upload(build(world), actor)

    async def over_mcp(world: World, mcp: Mcp, actor: str) -> ToolOutcome:
        return await mcp.upload(build(world), actor)

    return Write(
        f"upload/{id}",
        over_rest,
        over_mcp,
        lambda actor: upload_code(actor, allowed, allowed_result, denied_result),
    )


MANAGERS_OF_ALICE = frozenset({"owner", "admin"})
MANAGERS_OF_BOB = frozenset({"stranger", "admin"})
WRITE_SPECS = [
    manage_write("publish", DRAFT, "0.1.0", None),
    manage_write("publish", PUBLISHED, "2.0.0", None),
    manage_write("publish", PUBLISHED, "1.0.0", None),
    manage_write("publish", HIDDEN, "1.0.0", None),
    manage_write("publish", FOREIGN, "1.0.0", None),
    manage_write("publish", PUBLISHED, "9.9.9", "NOT_FOUND"),
    manage_write("publish", MISSING, "1.0.0", "NOT_FOUND"),
    manage_write("deprecate", PUBLISHED, "1.0.0", None),
    manage_write("deprecate", PUBLISHED, "0.9.0", None),
    manage_write("deprecate", PUBLISHED, "2.0.0", "CONFLICT"),
    manage_write("deprecate", FOREIGN, "1.0.0", "CONFLICT"),
    manage_write("deprecate", HIDDEN, "1.0.0", "CONFLICT"),
    manage_write("deprecate", PUBLISHED, "9.9.9", "NOT_FOUND"),
    manage_write("deprecate", MISSING, "1.0.0", "NOT_FOUND"),
    upload_write("a-new-entry", lambda world: make_skill_zip("matrix-new", "1.0.0"), WRITERS),
    upload_write(
        "a-new-version-of-an-entry-of-alice",
        lambda world: make_skill_zip(PUBLISHED, "4.0.0"),
        MANAGERS_OF_ALICE,
    ),
    upload_write(
        "a-new-version-of-an-entry-of-bob",
        lambda world: make_skill_zip(FOREIGN, "4.0.0"),
        MANAGERS_OF_BOB,
    ),
    upload_write(
        "a-new-version-of-a-hidden-entry",
        lambda world: make_grounding_zip(HIDDEN, "4.0.0"),
        MANAGERS_OF_ALICE,
    ),
    upload_write(
        "an-existing-version",
        lambda world: world.zip_of(PUBLISHED, "1.0.0"),
        MANAGERS_OF_ALICE,
        "CONFLICT",
    ),
    upload_write(
        "another-kind-under-an-existing-name",
        lambda world: make_grounding_zip(PUBLISHED, "4.0.0"),
        MANAGERS_OF_ALICE,
        "CONFLICT",
    ),
    upload_write(
        "something-that-is-not-a-package",
        lambda world: b"not a zip",
        WRITERS,
        "INVALID_PACKAGE",
        "INVALID_PACKAGE",
    ),
]


def check_outcomes(
    label: str, response: Response, result: ToolOutcome, wanted: str | None
) -> list[str]:
    over_rest, over_mcp = rest_outcome(response), mcp_outcome(result)
    problems = []
    if over_rest.code != wanted:
        problems.append(
            f"{label}: REST answered {over_rest.code} ({over_rest.status}), expected {wanted}"
        )
    if over_mcp.code != wanted:
        problems.append(f"{label}: MCP answered {over_mcp.code}, expected {wanted}")
    if over_rest.code is None and over_rest.status not in {200, 201}:
        problems.append(f"{label}: REST succeeded with status {over_rest.status}")
    if over_rest.code is not None and over_rest.status != STATUS_OF_CODE[over_rest.code]:
        problems.append(f"{label}: REST answered {over_rest.code} with status {over_rest.status}")
    if result.status not in {200, 401}:
        problems.append(f"{label}: MCP answered with the HTTP status {result.status}")
    if result.status == 401 and over_mcp.code != "UNAUTHORIZED":
        problems.append(f"{label}: MCP refused with 401 but says {over_mcp.code}")
    if wanted is not None and over_rest.code == over_mcp.code and result.status == 200:
        error = response.json()["error"]
        if (error["message"], error.get("reason")) != (result.message, result.reason):
            problems.append(f"{label}: REST says {error!r} but MCP says {result.text!r}")
    return problems


class TestAuthorizationMatrix:
    @pytest.mark.parametrize("build", read_specs())
    async def test_every_actor_gets_the_same_read_outcome_and_data_with_both_visibility_modes(
        self, world, rest, mcp, monkeypatch, build
    ):
        read = build(world)
        problems = []
        for public_read in (True, False):
            monkeypatch.setattr(settings.registry, "public_read", public_read)
            for actor in MATRIX_ACTORS:
                label = f"{read.id} as {actor} with public_read={public_read}"
                response, expected = await read.fetch(rest, actor)
                result = await mcp.call(read.tool, read.arguments, actor)
                wanted = read_code(actor, read.visible_to, public_read)
                problems += check_outcomes(label, response, result, wanted)
                if wanted is None and result.ok:
                    problems += [
                        f"{label}: {item}" for item in differences(expected, result.payload)
                    ]

        assert problems == []

    @pytest.mark.parametrize("write", WRITE_SPECS, ids=lambda write: write.id)
    async def test_every_actor_gets_the_same_write_outcome_with_both_visibility_modes(
        self, world, rest, mcp, monkeypatch, write
    ):
        problems = []
        for public_read in (True, False):
            monkeypatch.setattr(settings.registry, "public_read", public_read)
            for actor in MATRIX_ACTORS:
                label = f"{write.id} as {actor} with public_read={public_read}"
                response = await write.rest(world, rest, actor)
                if response.status_code < 300:
                    await world.reset()
                result = await write.mcp(world, mcp, actor)
                if result.ok:
                    await world.reset()
                problems += check_outcomes(label, response, result, write.expected(actor))

        assert problems == []
