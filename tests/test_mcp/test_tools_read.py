from __future__ import annotations

import inspect
import json
import uuid

import pytest
from mcp.types import CallToolResult

from app.config.settings import settings
from app.mcp import tools
from app.mcp.context import use_principal
from app.services.package_service import PackageService
from app.services.skill_service import SkillService
from app.services.version_service import VersionService
from tests.helpers.factories import (
    ACTOR_NAMES,
    ENTRY_OWNERS,
    VISIBLE_TO,
    Actors,
    seed_catalog,
)
from tests.test_mcp.support import (
    API,
    BINARY_BYTES,
    DRAFT,
    FOREIGN,
    GUIDE_TEXT,
    HIDDEN,
    LONG_TEXT,
    PDF,
    SCRIPT_TEXT,
    UNICODE_PATH,
    UNICODE_TEXT,
    Registry,
    payload_of,
    text_of,
)

ANONYMOUS = "anonymous"
PUBLIC_NAMES = [API, FOREIGN, PDF]


def assert_parity(result: CallToolResult, expected: object) -> None:
    assert result.is_error is False
    wrapped = {"result": expected} if isinstance(expected, list) else expected
    assert result.structured_content == wrapped
    assert json.loads(text_of(result)) == expected


def assert_failure(result: CallToolResult, prefix: str) -> None:
    assert result.is_error is True
    assert result.structured_content is None
    assert text_of(result).startswith(prefix), text_of(result)


def names_of(result: CallToolResult) -> list[str]:
    return [item["name"] for item in payload_of(result)["items"]]


class TestRegistry:
    def test_the_eleven_tools_of_the_contract_are_registered_in_order(self):
        assert [spec.name for spec in tools.TOOLS] == [
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

    def test_every_tool_is_an_async_function_named_after_its_registration(self):
        for spec in tools.TOOLS:
            assert inspect.iscoroutinefunction(spec.function)
            assert spec.function.__name__ == spec.name
            assert getattr(tools, spec.name) is spec.function

    def test_read_tools_are_marked_read_only_and_write_tools_are_not(self):
        read_only = {spec.name for spec in tools.TOOLS if spec.annotations.read_only_hint}
        assert read_only == {
            "search_skills",
            "get_skill",
            "get_entrypoint",
            "list_files",
            "read_file",
            "get_download_info",
            "list_tags",
            "list_categories",
        }
        for spec in tools.TOOLS:
            assert spec.annotations.open_world_hint is False
            if spec.name not in read_only:
                assert spec.annotations.destructive_hint is False

    @pytest.mark.parametrize(
        ("name", "mentions"),
        [
            ("search_skills", ["get_entrypoint", "get_skill", "total", "'grounding'", "SKILL.md"]),
            ("get_skill", ["get_entrypoint", "NOT_FOUND", "deprecated"]),
            ("get_entrypoint", ["SKILL.md", "INDEX.md", "'latest'", "list_files", "read_file"]),
            ("list_files", ["get_entrypoint", "'latest'", "manifest"]),
            (
                "read_file",
                ["list_files", "'latest'", "truncated", "INVALID_PATH", "get_download_info"],
            ),
            ("get_download_info", ["sha256", "requires_auth", "'latest'", "HTTP GET"]),
            ("list_tags", ["search_skills", "count"]),
            ("list_categories", ["search_skills", "count"]),
            (
                "upload_package",
                ["base64", "write scope", "SKILL.md", "INDEX.md", "publish_version"],
            ),
            ("publish_version", ["write scope", "draft", "'latest'"]),
            ("deprecate_version", ["write scope", "CONFLICT", "'latest'"]),
        ],
    )
    def test_descriptions_tell_an_agent_when_and_how_to_use_each_tool(self, name, mentions):
        spec = next(item for item in tools.TOOLS if item.name == name)
        assert len(spec.description) > 200
        for expected in mentions:
            assert expected in spec.description, (name, expected)


@pytest.mark.usefixtures("registry")
class TestSearchSkills:
    async def test_anonymous_callers_see_only_published_entries(self, session_factory):
        result = await tools.search_skills()

        assert names_of(result) == [API, FOREIGN, PDF]
        page = payload_of(result)
        assert (page["total"], page["limit"], page["offset"]) == (3, 10, 0)

    async def test_the_result_is_exactly_the_dto_of_the_service(self, actors, session_factory):
        async with session_factory() as db:
            expected = await SkillService(db).search(actors.owner, q="pdf", limit=10, offset=0)

        with use_principal(actors.owner):
            result = await tools.search_skills(query="pdf")

        assert_parity(result, expected.model_dump(mode="json"))

    @pytest.mark.parametrize(
        ("actor", "names"),
        [
            ("anonymous", [API, FOREIGN, PDF]),
            ("stranger", [API, FOREIGN, PDF]),
            ("owner", [API, FOREIGN, DRAFT, HIDDEN, PDF]),
            ("admin", [API, FOREIGN, DRAFT, HIDDEN, PDF]),
            ("owner_read", [API, FOREIGN, DRAFT, HIDDEN, PDF]),
        ],
    )
    async def test_owners_and_admins_also_see_drafts_and_hidden_entries(self, actors, actor, names):
        with use_principal(actors.get(actor)):
            result = await tools.search_skills()

        assert names_of(result) == names
        assert payload_of(result)["total"] == len(names)

    @pytest.mark.parametrize(
        ("arguments", "names"),
        [
            ({"kind": "grounding"}, [API]),
            ({"kind": "skill"}, [FOREIGN, PDF]),
            ({"tags": ["pdf"]}, [FOREIGN, PDF]),
            ({"tags": ["pdf", "forms"]}, [PDF]),
            ({"tags": ["pdf", "missing"]}, []),
            ({"category": "documents"}, [FOREIGN, PDF]),
            ({"category": "reference"}, [API]),
            ({"category": "nothing"}, []),
            ({"query": "pdf forms"}, [PDF]),
            ({"query": "ocr"}, [FOREIGN]),
            ({"query": "error codes"}, [API]),
            ({"query": "PDF", "kind": "skill", "category": "documents"}, [PDF, FOREIGN]),
            ({"query": "pdf", "tags": ["ocr"]}, [FOREIGN]),
        ],
    )
    async def test_filters_and_terms_narrow_the_results(self, arguments, names):
        result = await tools.search_skills(**arguments)

        assert names_of(result) == names

    async def test_results_are_ranked_by_relevance_when_there_is_a_query(self):
        assert names_of(await tools.search_skills(query="pdf")) == [PDF, FOREIGN]

    @pytest.mark.parametrize("hostile", ["%", "_", "\\", "%%", "\\%", "' OR '1'='1", "pdf%'; --"])
    async def test_like_wildcards_and_sql_fragments_match_nothing(self, hostile):
        result = await tools.search_skills(query=hostile)

        assert result.is_error is False
        assert payload_of(result)["total"] == 0

    async def test_pagination_walks_the_whole_result_in_order(self):
        seen = []
        for offset in range(3):
            result = await tools.search_skills(limit=1, offset=offset)
            page = payload_of(result)
            assert (page["total"], page["limit"], page["offset"]) == (3, 1, offset)
            seen.extend(item["name"] for item in page["items"])

        assert seen == [API, FOREIGN, PDF]
        beyond = payload_of(await tools.search_skills(limit=10, offset=3))
        assert (beyond["items"], beyond["total"]) == ([], 3)

    async def test_more_than_twenty_tags_match_nothing(self):
        result = await tools.search_skills(tags=[f"tag-{index}" for index in range(21)])

        assert payload_of(result)["total"] == 0

    async def test_service_validation_errors_are_reported_with_their_code(self):
        result = await tools.search_skills(limit=0)

        assert_failure(result, "VALIDATION_ERROR: limit must be an integer between 1 and 100")

    async def test_a_private_registry_asks_anonymous_callers_to_authenticate(
        self, monkeypatch, actors
    ):
        monkeypatch.setattr(settings.registry, "public_read", False)

        assert_failure(await tools.search_skills(), "UNAUTHORIZED: ")
        with use_principal(actors.stranger):
            assert names_of(await tools.search_skills()) == [API, FOREIGN, PDF]


@pytest.mark.usefixtures("registry")
class TestGetSkill:
    async def test_it_returns_the_detail_dto_with_the_visible_versions(
        self, actors, session_factory
    ):
        async with session_factory() as db:
            expected = await SkillService(db).get(None, PDF)

        result = await tools.get_skill(name=PDF)

        assert_parity(result, expected.model_dump(mode="json"))
        detail = payload_of(result)
        assert detail["latest_version"] == "1.1.0"
        assert [v["version"] for v in detail["versions"]] == [
            "2.0.0-beta.1",
            "1.1.0",
            "1.0.0",
            "0.9.0",
        ]
        statuses = {v["version"]: v["status"] for v in detail["versions"]}
        assert statuses["0.9.0"] == "deprecated"

    async def test_owners_also_see_their_draft_versions(self, actors):
        with use_principal(actors.owner):
            detail = payload_of(await tools.get_skill(name=PDF))

        assert detail["versions"][0]["version"] == "2.0.0"
        assert detail["versions"][0]["status"] == "draft"

    async def test_an_entry_can_be_found_by_its_uuid(self, registry: Registry):
        by_id = await tools.get_skill(name=registry.pdf_id)
        by_name = await tools.get_skill(name=PDF)

        assert by_id.structured_content == by_name.structured_content

    @pytest.mark.parametrize("name", [DRAFT, HIDDEN])
    async def test_drafts_and_hidden_entries_do_not_exist_for_strangers(self, actors, name):
        for actor in (None, actors.stranger, actors.stranger_read):
            with use_principal(actor):
                assert_failure(await tools.get_skill(name=name), "NOT_FOUND: ")

    @pytest.mark.parametrize(
        "name",
        [
            "does-not-exist",
            "../../etc/passwd",
            "'; DROP TABLE skills; --",
            "pdf-forms\x00",
            "PDF-FORMS",
            " pdf-forms",
            str(uuid.uuid4()),
            "",
            "ñandú",
            "x" * 100_000,
        ],
    )
    async def test_unknown_or_hostile_names_are_not_found_without_echoing_them_in_full(self, name):
        result = await tools.get_skill(name=name)

        assert_failure(result, "NOT_FOUND: Skill not found")
        assert len(text_of(result)) < 200


class TestGetSkillVisibilityMatrix:
    @pytest.mark.parametrize("actor", ACTOR_NAMES)
    @pytest.mark.parametrize("entry", sorted(VISIBLE_TO))
    async def test_visibility_follows_the_access_matrix(
        self, db_session, actors: Actors, actor, entry
    ):
        await seed_catalog(db_session, actors)

        with use_principal(actors.get(actor)):
            result = await tools.get_skill(name=entry)

        if actor in VISIBLE_TO[entry]:
            assert result.is_error is False
            assert payload_of(result)["owner_id"] == actors.get(ENTRY_OWNERS[entry]).user_id
        else:
            assert_failure(result, "NOT_FOUND: Skill not found")


@pytest.mark.usefixtures("registry")
class TestGetEntrypoint:
    async def test_a_skill_returns_its_skill_md_with_the_resolved_version_and_kind(
        self, session_factory, blob_store
    ):
        async with session_factory() as db:
            expected = await PackageService(db, blob_store).entrypoint(None, PDF, "latest")

        result = await tools.get_entrypoint(name=PDF)

        assert_parity(result, expected.model_dump(mode="json"))
        entry = payload_of(result)
        assert (entry["name"], entry["version"], entry["kind"]) == (PDF, "1.1.0", "skill")
        assert entry["path"] == "SKILL.md"
        assert entry["content"].startswith("---\n")
        assert "name: pdf-forms" in entry["content"]
        assert entry["truncated"] is False

    async def test_a_grounding_returns_its_index_md(self):
        entry = payload_of(await tools.get_entrypoint(name=API))

        assert (entry["path"], entry["kind"], entry["version"]) == (
            "INDEX.md",
            "grounding",
            "1.0.0",
        )

    @pytest.mark.parametrize(
        ("version", "resolved"),
        [
            ("latest", "1.1.0"),
            ("1.0.0", "1.0.0"),
            ("2.0.0-beta.1", "2.0.0-beta.1"),
            ("0.9.0", "0.9.0"),
        ],
    )
    async def test_versions_resolve_exactly_and_latest_prefers_stable_published(
        self, version, resolved
    ):
        entry = payload_of(await tools.get_entrypoint(name=PDF, version=version))

        assert entry["version"] == resolved

    @pytest.mark.parametrize(
        ("actor", "readable"),
        [
            ("anonymous", False),
            ("stranger", False),
            ("stranger_read", False),
            ("owner", True),
            ("owner_read", True),
            ("admin", True),
            ("admin_read", True),
        ],
    )
    async def test_draft_versions_are_only_readable_by_the_owner_and_admins(
        self, actors, actor, readable
    ):
        with use_principal(actors.get(actor)):
            result = await tools.get_entrypoint(name=PDF, version="2.0.0")

        if readable:
            assert payload_of(result)["version"] == "2.0.0"
        else:
            assert_failure(result, "NOT_FOUND: Version not found")

    async def test_latest_of_an_entry_without_published_versions_is_not_found(self, actors):
        with use_principal(actors.owner):
            result = await tools.get_entrypoint(name=DRAFT)

        assert_failure(result, "NOT_FOUND: The entry has no published version")

    @pytest.mark.parametrize("version", ["9.9.9", "1.0", "v1.0.0", "", "latest ", "LATEST", "../x"])
    async def test_unknown_versions_are_not_found(self, version):
        assert_failure(await tools.get_entrypoint(name=PDF, version=version), "NOT_FOUND: ")

    async def test_hidden_entries_are_invisible(self, actors):
        assert_failure(await tools.get_entrypoint(name=HIDDEN), "NOT_FOUND: ")
        with use_principal(actors.owner):
            assert payload_of(await tools.get_entrypoint(name=HIDDEN))["name"] == HIDDEN

    async def test_latest_follows_deprecations(self, actors, session_factory):
        async with session_factory() as db:
            await VersionService(db).deprecate(actors.owner, PDF, "1.1.0")

        assert payload_of(await tools.get_entrypoint(name=PDF))["version"] == "1.0.0"
        assert (
            payload_of(await tools.get_entrypoint(name=PDF, version="1.1.0"))["version"] == "1.1.0"
        )


@pytest.mark.usefixtures("registry")
class TestRepeatedReads:
    async def test_the_stored_package_is_opened_once_per_file_however_often_it_is_read(
        self, blob_store, monkeypatch
    ):
        opened: list[str] = []
        real_open = type(blob_store).open

        async def recording(self, key):
            opened.append(key)
            return await real_open(self, key)

        monkeypatch.setattr(type(blob_store), "open", recording)

        first = [await tools.get_entrypoint(name=PDF) for _ in range(3)]
        second = [await tools.read_file(name=PDF, path="docs/guide.md") for _ in range(3)]
        listing = [await tools.list_files(name=PDF) for _ in range(3)]

        assert all(result.is_error is False for result in [*first, *second, *listing])
        assert len(opened) == 2
        assert len({text_of(result) for result in first}) == 1
        assert payload_of(second[0])["content"] == GUIDE_TEXT


@pytest.mark.usefixtures("registry")
class TestListFiles:
    async def test_it_returns_the_manifest_dto_sorted_by_path(self, session_factory, blob_store):
        async with session_factory() as db:
            expected = await PackageService(db, blob_store).manifest(None, PDF, "latest")

        result = await tools.list_files(name=PDF)

        assert_parity(result, expected.model_dump(mode="json"))
        listing = payload_of(result)
        paths = [entry["path"] for entry in listing["files"]]
        assert (listing["name"], listing["version"]) == (PDF, "1.1.0")
        assert paths == sorted(paths)
        assert paths == [
            "SKILL.md",
            "data/table.bin",
            "docs/guide.md",
            "docs/long.txt",
            UNICODE_PATH,
            "scripts/fill.py",
        ]

    async def test_every_entry_tells_whether_it_can_be_read_inline(self):
        files = {
            entry["path"]: entry for entry in payload_of(await tools.list_files(name=PDF))["files"]
        }

        assert files["docs/guide.md"]["text"] is True
        assert files["docs/guide.md"]["media_type"] == "text/markdown"
        assert files["data/table.bin"]["text"] is False
        assert files["data/table.bin"]["size"] == len(BINARY_BYTES)
        assert len(files["scripts/fill.py"]["sha256"]) == 64

    async def test_the_listing_of_an_exact_version_resolves_that_version(self):
        listing = payload_of(await tools.list_files(name=API, version="1.0.0"))

        assert listing["version"] == "1.0.0"
        assert [entry["path"] for entry in listing["files"]] == [
            "INDEX.md",
            "schemas/api.json",
            "tables/codes.csv",
        ]

    @pytest.mark.parametrize("name", [DRAFT, HIDDEN, "unknown"])
    async def test_unavailable_entries_are_not_found(self, name):
        assert_failure(await tools.list_files(name=name), "NOT_FOUND: ")


@pytest.mark.usefixtures("registry")
class TestReadFile:
    async def test_a_text_file_comes_back_inline(self, session_factory, blob_store):
        async with session_factory() as db:
            expected = await PackageService(db, blob_store).read_file(
                None, PDF, "latest", "docs/guide.md"
            )

        result = await tools.read_file(name=PDF, path="docs/guide.md")

        assert_parity(result, expected.model_dump(mode="json"))
        content = payload_of(result)
        assert content["content"] == GUIDE_TEXT
        assert (content["text"], content["truncated"], content["encoding"]) == (
            True,
            False,
            "utf-8",
        )

    async def test_scripts_and_non_ascii_paths_are_readable(self):
        script = payload_of(await tools.read_file(name=PDF, path="scripts/fill.py"))
        accented = payload_of(await tools.read_file(name=PDF, path=UNICODE_PATH))

        assert script["content"] == SCRIPT_TEXT
        assert accented["content"] == UNICODE_TEXT

    async def test_a_binary_file_only_returns_its_metadata(self):
        content = payload_of(await tools.read_file(name=PDF, path="data/table.bin"))

        assert content["text"] is False
        assert content["content"] is None
        assert content["size"] == len(BINARY_BYTES)

    async def test_a_long_text_is_cut_at_the_inline_limit_and_flagged(self, monkeypatch):
        monkeypatch.setattr(settings.registry.limits, "max_inline_read_bytes", 100)

        content = payload_of(await tools.read_file(name=PDF, path="docs/long.txt"))

        assert content["truncated"] is True
        assert content["content"] == LONG_TEXT[:100]
        assert content["size"] == len(LONG_TEXT)

    async def test_an_exact_version_is_read_from_that_version(self):
        content = payload_of(await tools.read_file(name=PDF, path="SKILL.md", version="1.0.0"))

        assert "version: 1.0.0" in content["content"]

    @pytest.mark.parametrize(
        "path",
        [
            "../etc/passwd",
            "/etc/passwd",
            "docs/../../etc/passwd",
            "docs/../docs/guide.md",
            "./docs/guide.md",
            "docs//guide.md",
            "docs\\guide.md",
            "..\\..\\windows\\system32",
            "C:\\Windows\\win.ini",
            "docs/guide.md\x00.png",
            "\x00",
            "docs/\x1b[31mguide.md",
            "",
            "docs/",
            "..",
            ".",
        ],
    )
    async def test_paths_that_escape_the_package_are_rejected_with_invalid_path(self, path):
        result = await tools.read_file(name=PDF, path=path)

        assert_failure(result, "VALIDATION_ERROR: ")
        assert text_of(result).endswith(" [INVALID_PATH]")

    @pytest.mark.parametrize(
        "path",
        [
            "missing.md",
            "DOCS/GUIDE.MD",
            "docs/guide.md ",
            "%2e%2e/etc/passwd",
            "docs/guide.md%00",
            "docs/Guide.md",
            "SKILL.md/",
            "scripts",
            "x" * 5000,
        ],
    )
    async def test_paths_outside_the_manifest_are_not_found(self, path):
        result = await tools.read_file(name=PDF, path=path)

        assert result.is_error is True
        assert text_of(result).split(":", 1)[0] in {"NOT_FOUND", "VALIDATION_ERROR"}
        assert len(text_of(result)) < 400

    @pytest.mark.parametrize("name", [DRAFT, HIDDEN, "unknown"])
    async def test_files_of_unavailable_entries_are_not_found(self, name):
        assert_failure(await tools.read_file(name=name, path="SKILL.md"), "NOT_FOUND: ")


@pytest.mark.usefixtures("registry")
class TestGetDownloadInfo:
    async def test_it_returns_an_absolute_rest_url_with_the_package_hash(
        self, monkeypatch, session_factory, blob_store
    ):
        monkeypatch.setattr(settings.server, "public_url", "https://registry.example.com")
        async with session_factory() as db:
            expected = await PackageService(db, blob_store).download_info(None, PDF, "latest")

        result = await tools.get_download_info(name=PDF)

        assert_parity(result, expected.model_dump(mode="json"))
        info = payload_of(result)
        assert info["url"] == (
            "https://registry.example.com/api/v1/skills/pdf-forms/versions/1.1.0/download"
        )
        assert info["version"] == "1.1.0"
        assert info["requires_auth"] is False
        assert len(info["sha256"]) == 64
        assert info["size_bytes"] > 0

    async def test_the_hash_is_the_one_of_the_stored_package(self, blob_store):
        info = payload_of(await tools.get_download_info(name=PDF, version="1.0.0"))

        assert await blob_store.exists(info["sha256"])
        assert len(await blob_store.get(info["sha256"])) == info["size_bytes"]

    async def test_drafts_and_private_registries_require_authentication(self, actors, monkeypatch):
        with use_principal(actors.owner):
            draft = payload_of(await tools.get_download_info(name=PDF, version="2.0.0"))
        assert draft["requires_auth"] is True

        monkeypatch.setattr(settings.registry, "public_read", False)
        with use_principal(actors.stranger):
            private = payload_of(await tools.get_download_info(name=PDF))
        assert private["requires_auth"] is True

    async def test_the_public_url_may_end_with_a_slash(self, monkeypatch):
        monkeypatch.setattr(settings.server, "public_url", "http://localhost:9000/")

        info = payload_of(await tools.get_download_info(name=API))

        assert info["url"].startswith("http://localhost:9000/api/v1/skills/api-reference/")

    @pytest.mark.parametrize("name", [DRAFT, HIDDEN, "unknown"])
    async def test_unavailable_entries_are_not_found(self, name):
        assert_failure(await tools.get_download_info(name=name), "NOT_FOUND: ")


@pytest.mark.usefixtures("registry")
class TestTaxonomy:
    async def test_tags_are_counted_over_visible_entries_most_used_first(self, session_factory):
        async with session_factory() as db:
            expected = await SkillService(db).tags(None, limit=50)

        result = await tools.list_tags()

        assert_parity(result, [item.model_dump(mode="json") for item in expected])
        assert payload_of(result) == [
            {"name": "pdf", "count": 2},
            {"name": "api", "count": 1},
            {"name": "forms", "count": 1},
            {"name": "ocr", "count": 1},
            {"name": "reference", "count": 1},
        ]

    async def test_categories_are_counted_the_same_way(self):
        result = await tools.list_categories()

        assert payload_of(result) == [
            {"name": "documents", "count": 2},
            {"name": "reference", "count": 1},
        ]
        assert result.structured_content == {"result": payload_of(result)}

    async def test_kind_and_limit_restrict_the_listing(self):
        grounding = payload_of(await tools.list_tags(kind="grounding"))
        limited = payload_of(await tools.list_tags(limit=2))
        categories = payload_of(await tools.list_categories(kind="grounding", limit=1))

        assert grounding == [{"name": "api", "count": 1}, {"name": "reference", "count": 1}]
        assert limited == [{"name": "pdf", "count": 2}, {"name": "api", "count": 1}]
        assert categories == [{"name": "reference", "count": 1}]

    async def test_the_limit_is_validated_by_the_service(self):
        assert_failure(await tools.list_tags(limit=0), "VALIDATION_ERROR: ")
        assert_failure(await tools.list_categories(limit=201), "VALIDATION_ERROR: ")

    async def test_private_registries_hide_the_taxonomy_from_anonymous_callers(self, monkeypatch):
        monkeypatch.setattr(settings.registry, "public_read", False)

        assert_failure(await tools.list_tags(), "UNAUTHORIZED: ")
        assert_failure(await tools.list_categories(), "UNAUTHORIZED: ")
