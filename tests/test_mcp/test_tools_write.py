from __future__ import annotations

import base64
import random
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config.settings import settings
from app.mcp import tools
from app.mcp.context import use_principal
from app.models import Skill, SkillVersion
from app.services import package_service
from app.services.errors import ConflictError, PayloadTooLargeError
from app.services.package_service import base64_length
from app.storage import LocalBlobStore
from tests.helpers.factories import (
    ACTOR_NAMES,
    SEEDED_VERSIONS,
    Actors,
    seed_catalog,
    write_outcome,
)
from tests.helpers.packages import (
    index_md,
    make_bomb_zip,
    make_encrypted_zip,
    make_grounding_zip,
    make_skill_zip,
    make_symlink_zip,
    make_traversal_zip,
    make_zip,
    skill_md,
)
from tests.test_mcp.support import DRAFT, PDF, payload_of, text_of, zip_to_base64
from tests.test_mcp.test_tools_read import assert_failure


async def counts(session_factory: async_sessionmaker[AsyncSession]) -> tuple[int, int]:
    async with session_factory() as db:
        skills = (await db.execute(select(func.count()).select_from(Skill))).scalar_one()
        versions = (await db.execute(select(func.count()).select_from(SkillVersion))).scalar_one()
    return skills, versions


def stored_blobs(store: LocalBlobStore) -> list[str]:
    return sorted(path.stem for path in store.root.rglob("*.zip")) if store.root.exists() else []


async def upload(actors: Actors, actor: str, data: bytes, **arguments):
    with use_principal(actors.get(actor)):
        return await tools.upload_package(content_base64=zip_to_base64(data), **arguments)


class TestUploadPackage:
    async def test_a_new_package_is_stored_as_a_draft_owned_by_the_uploader(self, actors):
        data = make_skill_zip("new-skill", "0.1.0", tags=["fresh"], files={"notes.txt": "hello"})

        result = await upload(actors, "owner", data)

        assert result.is_error is False
        body = payload_of(result)
        assert result.structured_content == body
        assert set(body) == {"skill", "version"}
        assert body["skill"]["name"] == "new-skill"
        assert body["skill"]["owner_id"] == actors.owner.user_id
        assert body["skill"]["status"] == "draft"
        assert body["skill"]["latest_version"] is None
        assert body["version"]["status"] == "draft"
        assert body["version"]["version"] == "0.1.0"
        assert body["version"]["uploaded_by"] == actors.owner.user_id
        assert body["version"]["published_by"] is None

    async def test_the_upload_result_matches_what_get_skill_reports_afterwards(self, actors):
        body = payload_of(await upload(actors, "owner", make_skill_zip("twin-skill", "1.0.0")))

        with use_principal(actors.owner):
            detail = payload_of(await tools.get_skill(name="twin-skill"))

        assert detail["versions"] == [body["version"]]
        assert {key: detail[key] for key in body["skill"]} == body["skill"]

    async def test_publish_makes_the_version_public_and_latest_immediately(self, actors):
        data = make_skill_zip("public-skill", "1.2.0")

        body = payload_of(await upload(actors, "owner", data, publish=True))

        assert body["version"]["status"] == "published"
        assert body["version"]["published_by"] == actors.owner.user_id
        assert body["skill"]["status"] == "published"
        assert body["skill"]["latest_version"] == "1.2.0"
        assert payload_of(await tools.get_skill(name="public-skill"))["name"] == "public-skill"

    async def test_drafts_are_invisible_to_everyone_but_their_owner(self, actors):
        await upload(actors, "owner", make_skill_zip("quiet-skill", "1.0.0"))

        assert_failure(await tools.get_skill(name="quiet-skill"), "NOT_FOUND: ")
        with use_principal(actors.owner):
            assert payload_of(await tools.get_skill(name="quiet-skill"))["status"] == "draft"

    async def test_a_grounding_package_is_detected_from_its_index_md(self, actors):
        body = payload_of(await upload(actors, "owner", make_grounding_zip("new-ground", "1.0.0")))

        assert body["skill"]["kind"] == "grounding"
        assert body["version"]["entrypoint"] == "INDEX.md"

    async def test_the_version_can_be_given_as_an_argument_instead_of_the_frontmatter(self, actors):
        data = make_skill_zip("late-version", None)

        body = payload_of(await upload(actors, "owner", data, version="2.1.0"))

        assert body["version"]["version"] == "2.1.0"

    async def test_a_version_that_contradicts_the_frontmatter_is_rejected(self, actors):
        result = await upload(actors, "owner", make_skill_zip("clash", "1.0.0"), version="1.0.1")

        assert_failure(result, "INVALID_PACKAGE: ")
        assert text_of(result).endswith(" [VERSION_MISMATCH]")

    async def test_a_wrapper_directory_and_macos_junk_are_tolerated(self, actors):
        data = make_skill_zip("wrapped-skill", "1.0.0", wrapper="wrapped-skill", macos_junk=True)

        body = payload_of(await upload(actors, "owner", data, publish=True))

        assert body["version"]["file_count"] == 1

    async def test_the_uploaded_package_can_be_read_back_byte_for_byte(self, actors, blob_store):
        data = make_skill_zip("round-trip", "1.0.0", files={"data/blob.bin": bytes(range(256))})

        body = payload_of(await upload(actors, "owner", data, publish=True))

        info = payload_of(await tools.get_download_info(name="round-trip"))
        assert info["sha256"] == body["version"]["sha256"]
        assert await blob_store.get(info["sha256"]) == data

    async def test_the_same_version_cannot_be_uploaded_twice(self, actors, session_factory):
        await upload(actors, "owner", make_skill_zip("once-only", "1.0.0"))
        before = await counts(session_factory)

        result = await upload(
            actors, "owner", make_skill_zip("once-only", "1.0.0", description="x")
        )

        assert_failure(result, "CONFLICT: Version 1.0.0 of 'once-only' already exists")
        assert await counts(session_factory) == before

    async def test_new_versions_of_an_entry_are_added_by_its_owner_and_by_admins(self, actors):
        await upload(actors, "owner", make_skill_zip("shared-skill", "1.0.0"), publish=True)

        by_owner = payload_of(
            await upload(actors, "owner", make_skill_zip("shared-skill", "1.1.0"))
        )
        by_admin = payload_of(
            await upload(actors, "admin", make_skill_zip("shared-skill", "1.2.0"))
        )

        assert by_owner["version"]["status"] == "draft"
        assert by_admin["version"]["uploaded_by"] == actors.admin.user_id
        assert by_admin["skill"]["owner_id"] == actors.owner.user_id

    async def test_other_users_cannot_add_versions_to_somebody_elses_entry(
        self, actors, session_factory
    ):
        await upload(actors, "owner", make_skill_zip("mine-only", "1.0.0"), publish=True)
        before = await counts(session_factory)

        result = await upload(actors, "stranger", make_skill_zip("mine-only", "1.1.0"))

        assert_failure(result, "FORBIDDEN: 'mine-only' belongs to another user")
        assert await counts(session_factory) == before

    async def test_the_kind_of_an_entry_never_changes(self, actors):
        await upload(actors, "owner", make_skill_zip("fixed-kind", "1.0.0"))

        result = await upload(actors, "owner", make_grounding_zip("fixed-kind", "2.0.0"))

        assert_failure(result, "CONFLICT: ")

    async def test_anonymous_callers_must_authenticate(self, actors, session_factory):
        result = await upload(actors, "anonymous", make_skill_zip("anon-skill", "1.0.0"))

        assert_failure(result, "UNAUTHORIZED: ")
        assert await counts(session_factory) == (0, 0)

    @pytest.mark.parametrize("actor", ["owner_read", "stranger_read", "admin_read"])
    async def test_read_only_credentials_cannot_upload(self, actors, session_factory, actor):
        result = await upload(actors, actor, make_skill_zip("read-only", "1.0.0"), publish=True)

        assert_failure(result, "FORBIDDEN: This credential has read-only scope")
        assert await counts(session_factory) == (0, 0)

    @pytest.mark.parametrize(
        ("actor", "prefix"),
        [("anonymous", "UNAUTHORIZED: "), ("owner_read", "FORBIDDEN: ")],
    )
    async def test_authorization_is_checked_before_the_payload_is_looked_at(
        self, actors, monkeypatch, actor, prefix
    ):
        monkeypatch.setattr(tools, "base64", SimpleNamespace(b64decode=pytest.fail))
        with use_principal(actors.get(actor)):
            garbage = await tools.upload_package(content_base64="!!! not base64 !!!")
            oversize = await tools.upload_package(content_base64="A" * 10_000_000)

        assert_failure(garbage, prefix)
        assert_failure(oversize, prefix)

    async def test_the_registry_default_applies_when_publish_is_false(self, actors, monkeypatch):
        monkeypatch.setattr(settings.registry, "auto_publish", True)

        body = payload_of(await upload(actors, "owner", make_skill_zip("auto-skill", "1.0.0")))

        assert body["version"]["status"] == "published"

    @pytest.mark.parametrize(
        ("label", "build", "reason"),
        [
            ("zip slip", lambda: make_traversal_zip("../evil.txt"), "UNSAFE_PATH"),
            ("deep zip slip", lambda: make_traversal_zip("a/../../evil.txt"), "UNSAFE_PATH"),
            ("absolute path", lambda: make_traversal_zip("/etc/passwd"), "UNSAFE_PATH"),
            ("backslash path", lambda: make_traversal_zip("..\\evil.txt"), "UNSAFE_PATH"),
            ("symlink", lambda: make_symlink_zip(), "SYMLINK_NOT_ALLOWED"),
            ("encrypted", lambda: make_encrypted_zip(), "ENCRYPTED_ENTRY"),
            ("not a zip", lambda: b"this is plain text", "NOT_A_ZIP"),
            ("empty file", lambda: b"", "NOT_A_ZIP"),
            ("truncated zip", lambda: make_skill_zip("cut", "1.0.0")[:-30], "CORRUPT_ARCHIVE"),
            ("no entrypoint", lambda: make_zip({"README.md": "# readme"}), "MISSING_ENTRYPOINT"),
            (
                "both entrypoints",
                lambda: make_zip({"SKILL.md": skill_md("both"), "INDEX.md": index_md("both")}),
                "AMBIGUOUS_ENTRYPOINT",
            ),
            (
                "no frontmatter",
                lambda: make_zip({"SKILL.md": "# just markdown\n"}),
                "BAD_FRONTMATTER",
            ),
            (
                "yaml alias",
                lambda: make_zip(
                    {
                        "SKILL.md": "---\nname: aliased\nversion: 1.0.0\n"
                        "description: &shared text\nnotes: *shared\n---\nbody\n"
                    }
                ),
                "BAD_FRONTMATTER",
            ),
            (
                "duplicate paths",
                lambda: make_zip([("SKILL.md", skill_md("dup")), ("a.txt", "1"), ("a.txt", "2")]),
                "DUPLICATE_PATH",
            ),
            ("invalid name", lambda: make_skill_zip("Not Valid", "1.0.0"), "INVALID_FIELD"),
            (
                "uuid shaped name",
                lambda: make_skill_zip("123e4567-e89b-12d3-a456-426614174000", "1.0.0"),
                "INVALID_FIELD",
            ),
            ("missing version", lambda: make_skill_zip("no-version", None), "MISSING_FIELD"),
            (
                "kind mismatch",
                lambda: make_skill_zip("kinds", "1.0.0", kind="grounding"),
                "KIND_MISMATCH",
            ),
        ],
    )
    async def test_hostile_or_malformed_packages_are_rejected_with_their_reason_and_leave_no_trace(
        self, actors, session_factory, blob_store, label, build, reason
    ):
        result = await upload(actors, "owner", build(), publish=True)

        assert_failure(result, "INVALID_PACKAGE: ")
        assert text_of(result).endswith(f" [{reason}]"), label
        assert "Traceback" not in text_of(result)
        assert await counts(session_factory) == (0, 0)
        assert stored_blobs(blob_store) == []

    @pytest.mark.parametrize("declared_size", [None, 10])
    async def test_a_decompression_bomb_is_rejected_even_with_a_forged_size(
        self, actors, monkeypatch, session_factory, blob_store, declared_size
    ):
        monkeypatch.setattr(settings.registry.limits, "max_uncompressed_bytes", 2 * 1024 * 1024)
        monkeypatch.setattr(settings.registry.limits, "max_file_bytes", 1024 * 1024)
        bomb = make_bomb_zip(8 * 1024 * 1024, declared_size=declared_size)
        assert len(bomb) < 64 * 1024

        result = await upload(actors, "owner", bomb)

        assert_failure(result, "INVALID_PACKAGE: ")
        assert text_of(result).endswith(" [TOO_LARGE]")
        assert await counts(session_factory) == (0, 0)
        assert stored_blobs(blob_store) == []

    async def test_the_file_count_limit_of_the_registry_applies(self, actors, monkeypatch):
        monkeypatch.setattr(settings.registry.limits, "max_files", 2)
        data = make_skill_zip("many-files", "1.0.0", files={"a": "1", "b": "2", "c": "3"})

        result = await upload(actors, "owner", data)

        assert text_of(result).endswith(" [TOO_MANY_FILES]")

    @pytest.mark.parametrize(
        "mangle",
        [
            lambda text: "not base64 at all!",
            lambda text: text[:20] + "\n" + text[20:],
            lambda text: text[:20] + " " + text[20:],
            lambda text: text + "\n",
            lambda text: text.replace("+", "-").replace("/", "_") + "!",
            lambda text: text.rstrip("=")[:-1],
            lambda text: text + "QUJD",
            lambda text: text + "=" * 3,
            lambda text: text + "ñ",
            lambda text: text[:10] + "\x00" + text[10:],
            lambda text: " " + text,
            lambda text: "data:application/zip;base64," + text,
        ],
    )
    async def test_malformed_base64_is_rejected_before_any_package_work(
        self, actors, session_factory, blob_store, mangle
    ):
        encoded = zip_to_base64(make_skill_zip("b64-skill", "1.0.0", files={"x.txt": "xyz"}))
        assert encoded.endswith("=") or len(encoded) % 4 == 0

        with use_principal(actors.owner):
            result = await tools.upload_package(content_base64=mangle(encoded))

        assert_failure(result, "VALIDATION_ERROR: content_base64 must be valid base64")
        assert await counts(session_factory) == (0, 0)
        assert stored_blobs(blob_store) == []

    async def test_urlsafe_base64_is_not_accepted(self, actors):
        data = make_skill_zip(
            "urlsafe", "1.0.0", files={"blob.bin": bytes([0xFB, 0xFF, 0xBF] * 40)}
        )
        encoded = base64.urlsafe_b64encode(data).decode("ascii")
        assert "-" in encoded or "_" in encoded

        with use_principal(actors.owner):
            result = await tools.upload_package(content_base64=encoded)

        assert_failure(result, "VALIDATION_ERROR: content_base64 must be valid base64")

    async def test_an_encoding_longer_than_the_limit_is_rejected_before_decoding(
        self, actors, monkeypatch
    ):
        monkeypatch.setattr(settings.registry.limits, "max_package_bytes", 1000)
        monkeypatch.setattr(tools, "base64", SimpleNamespace(b64decode=pytest.fail))
        longest = base64_length(1000)

        with use_principal(actors.owner):
            over = await tools.upload_package(content_base64="!" * (longest + 1))

        assert_failure(over, "PAYLOAD_TOO_LARGE: The package exceeds the limit of 1000 bytes")

    async def test_a_real_package_over_the_limit_is_reported_as_too_large(
        self, actors, monkeypatch, session_factory, blob_store
    ):
        monkeypatch.setattr(settings.registry.limits, "max_package_bytes", 1000)
        noise = random.Random(7).randbytes(4000)
        data = make_skill_zip("too-big", "1.0.0", files={"noise.bin": noise})
        assert len(data) > 1000

        result = await upload(actors, "owner", data)

        assert_failure(result, "PAYLOAD_TOO_LARGE: The package exceeds the limit of 1000 bytes")
        assert await counts(session_factory) == (0, 0)
        assert stored_blobs(blob_store) == []

    async def test_the_prefilter_is_derived_from_the_single_encoded_length_helper(
        self, actors, monkeypatch
    ):
        monkeypatch.setattr(package_service, "base64_length", lambda size: 6_000_000 + size)
        monkeypatch.setattr(settings.registry.limits, "max_package_bytes", 1000)
        monkeypatch.setattr(tools, "base64", SimpleNamespace(b64decode=lambda *a, **k: b"stub"))

        assert tools.decode_package("A" * 6_001_000) == b"stub"
        with pytest.raises(PayloadTooLargeError, match="limit of 1000 bytes"):
            tools.decode_package("A" * 6_001_001)

    @pytest.mark.parametrize("excess", [0, 1, 2])
    async def test_the_prefilter_never_rejects_what_the_limit_allows(
        self, actors, monkeypatch, excess
    ):
        limit = 1000
        monkeypatch.setattr(settings.registry.limits, "max_package_bytes", limit)
        data = b"x" * (limit + excess)

        with use_principal(actors.owner):
            result = await tools.upload_package(content_base64=zip_to_base64(data))

        if excess == 0:
            assert_failure(result, "INVALID_PACKAGE: ")
            assert text_of(result).endswith(" [NOT_A_ZIP]")
        else:
            assert_failure(result, "PAYLOAD_TOO_LARGE: ")

    @pytest.mark.parametrize("size", [0, 1, 2, 3, 4, 5, 999, 1000, 1001, 1002, 1003, 1004])
    @pytest.mark.parametrize("limit", [1, 2, 3, 1000, 25 * 1024 * 1024])
    def test_the_prefilter_never_rejects_a_package_the_limit_allows(self, size, limit):
        encoded = len(base64.b64encode(b"x" * size))

        if encoded > base64_length(limit):
            assert size > limit

    @pytest.mark.parametrize("limit", [1, 2, 3, 4, 1000, 26214400])
    def test_the_prefilter_cuts_exactly_one_group_above_the_longest_valid_encoding(self, limit):
        assert base64_length(limit) == len(base64.b64encode(b"x" * limit))
        assert base64_length(limit + 3) == base64_length(limit) + 4


@pytest.mark.usefixtures("registry")
class TestPublishVersion:
    async def test_the_owner_publishes_a_draft_and_it_becomes_latest(self, actors):
        with use_principal(actors.owner):
            result = await tools.publish_version(name=PDF, version="2.0.0")
            detail = payload_of(await tools.get_skill(name=PDF))

        body = payload_of(result)
        assert result.structured_content == body
        assert (body["version"], body["status"]) == ("2.0.0", "published")
        assert body["published_by"] == actors.owner.user_id
        assert body["published_at"] is not None
        assert detail["latest_version"] == "2.0.0"
        assert payload_of(await tools.get_entrypoint(name=PDF))["version"] == "2.0.0"

    async def test_publishing_twice_changes_nothing_the_second_time(self, actors):
        with use_principal(actors.owner):
            first = payload_of(await tools.publish_version(name=PDF, version="2.0.0"))
            second = payload_of(await tools.publish_version(name=PDF, version="2.0.0"))

        assert first == second

    async def test_the_first_published_version_makes_the_entry_visible(self, actors):
        assert_failure(await tools.get_skill(name=DRAFT), "NOT_FOUND: ")

        with use_principal(actors.owner):
            await tools.publish_version(name=DRAFT, version="0.1.0")

        assert payload_of(await tools.get_skill(name=DRAFT))["status"] == "published"

    async def test_a_deprecated_version_can_be_published_again(self, actors):
        with use_principal(actors.owner):
            body = payload_of(await tools.publish_version(name=PDF, version="0.9.0"))

        assert body["status"] == "published"
        assert body["deprecated_at"] is None

    async def test_admins_can_publish_but_read_only_admins_cannot(self, actors):
        with use_principal(actors.admin_read):
            assert_failure(await tools.publish_version(name=PDF, version="2.0.0"), "FORBIDDEN: ")
        with use_principal(actors.admin):
            body = payload_of(await tools.publish_version(name=PDF, version="2.0.0"))

        assert body["published_by"] == actors.admin.user_id

    @pytest.mark.parametrize(
        ("name", "version", "prefix"),
        [
            (PDF, "latest", "NOT_FOUND: Version not found"),
            (PDF, "9.9.9", "NOT_FOUND: Version not found"),
            (PDF, "", "NOT_FOUND: Version not found"),
            ("unknown-skill", "1.0.0", "NOT_FOUND: Skill not found"),
            ("../../etc", "1.0.0", "NOT_FOUND: Skill not found"),
        ],
    )
    async def test_unknown_targets_are_not_found(self, actors, name, version, prefix):
        with use_principal(actors.owner):
            assert_failure(await tools.publish_version(name=name, version=version), prefix)


class TestPublishVersionMatrix:
    @pytest.mark.parametrize("actor", ACTOR_NAMES)
    @pytest.mark.parametrize("entry", sorted(SEEDED_VERSIONS))
    async def test_the_outcome_follows_the_authorization_matrix(
        self, db_session, actors, actor, entry
    ):
        await seed_catalog(db_session, actors)

        with use_principal(actors.get(actor)):
            result = await tools.publish_version(name=entry, version=SEEDED_VERSIONS[entry])

        expected = write_outcome(actor, entry)
        if expected is None:
            assert result.is_error is False
            assert payload_of(result)["status"] == "published"
        else:
            assert_failure(result, f"{expected.code}: ")


@pytest.mark.usefixtures("registry")
class TestDeprecateVersion:
    async def test_a_published_version_is_deprecated_and_stops_being_latest(self, actors):
        with use_principal(actors.owner):
            result = await tools.deprecate_version(name=PDF, version="1.1.0")
            latest = payload_of(await tools.get_entrypoint(name=PDF))
            pinned = payload_of(await tools.get_entrypoint(name=PDF, version="1.1.0"))

        body = payload_of(result)
        assert result.structured_content == body
        assert (body["version"], body["status"]) == ("1.1.0", "deprecated")
        assert body["deprecated_at"] is not None
        assert latest["version"] == "1.0.0"
        assert pinned["version"] == "1.1.0"

    async def test_deprecating_twice_changes_nothing_the_second_time(self, actors):
        with use_principal(actors.owner):
            first = payload_of(await tools.deprecate_version(name=PDF, version="1.1.0"))
            second = payload_of(await tools.deprecate_version(name=PDF, version="1.1.0"))

        assert first == second

    async def test_the_only_published_version_cannot_be_deprecated(self, actors):
        with use_principal(actors.owner):
            result = await tools.deprecate_version(name="api-reference", version="1.0.0")
            detail = payload_of(await tools.get_skill(name="api-reference"))

        assert_failure(result, "CONFLICT: The only published version cannot be deprecated")
        assert detail["latest_version"] == "1.0.0"

    async def test_a_draft_cannot_be_deprecated(self, actors):
        with use_principal(actors.owner):
            result = await tools.deprecate_version(name=PDF, version="2.0.0")

        assert_failure(result, "CONFLICT: Only a published version can be deprecated")

    @pytest.mark.parametrize(
        ("name", "version", "prefix"),
        [
            (PDF, "latest", "NOT_FOUND: Version not found"),
            (PDF, "9.9.9", "NOT_FOUND: Version not found"),
            ("unknown-skill", "1.0.0", "NOT_FOUND: Skill not found"),
        ],
    )
    async def test_unknown_targets_are_not_found(self, actors, name, version, prefix):
        with use_principal(actors.owner):
            assert_failure(await tools.deprecate_version(name=name, version=version), prefix)


class TestDeprecateVersionMatrix:
    @pytest.mark.parametrize("actor", ACTOR_NAMES)
    @pytest.mark.parametrize("entry", sorted(SEEDED_VERSIONS))
    async def test_the_outcome_follows_the_authorization_matrix(
        self, db_session, actors, actor, entry
    ):
        await seed_catalog(db_session, actors)

        with use_principal(actors.get(actor)):
            result = await tools.deprecate_version(name=entry, version=SEEDED_VERSIONS[entry])

        expected = write_outcome(actor, entry)
        if expected is None and entry != "pub-skill":
            expected = ConflictError
        if expected is None:
            assert result.is_error is False
            assert payload_of(result)["status"] == "deprecated"
        else:
            assert_failure(result, f"{expected.code}: ")
