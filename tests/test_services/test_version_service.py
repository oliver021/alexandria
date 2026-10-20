from __future__ import annotations

import asyncio
import logging
import random

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config.settings import settings
from app.db.base import Base
from app.db.session import configure_sqlite_engine
from app.models import SkillStatus, VersionStatus
from app.models.schemas import VersionDetail, VersionSummary
from app.repositories import skill_repo
from app.services import catalog as catalog_module
from app.services import mappers
from app.services.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
)
from app.services.package_service import PackageService
from app.services.skill_service import SkillService
from app.services.version_service import VersionService
from tests.helpers.factories import (
    ACTOR_NAMES,
    ENTRY_OWNERS,
    PRIVILEGED_ACTORS,
    SEEDED_VERSIONS,
    VISIBLE_TO,
    add_skill,
    add_version,
    make_user,
    principal_of,
    record_writes,
    seed_catalog,
    upload_package,
    write_outcome,
)

PUBLIC_URL = "https://registry.example.com"

WRITE_CASES = [
    (actor, entry, write_outcome(actor, entry)) for entry in ENTRY_OWNERS for actor in ACTOR_NAMES
]
WRITE_IDS = [f"{actor}-{entry}" for actor, entry, _ in WRITE_CASES]


def deprecate_outcome(actor: str, entry: str):
    outcome = write_outcome(actor, entry)
    if outcome is not None:
        return outcome
    return None if entry == "pub-skill" else ConflictError


DEPRECATE_CASES = [
    (actor, entry, deprecate_outcome(actor, entry))
    for entry in ENTRY_OWNERS
    for actor in ACTOR_NAMES
]
SEEDED_DEPRECABLE = {**SEEDED_VERSIONS, "pub-skill": "1.0.0"}


@pytest_asyncio.fixture
async def catalog(db_session, actors):
    return await seed_catalog(db_session, actors)


@pytest.fixture
def service(db_session):
    return VersionService(db_session, public_read=True, public_url=PUBLIC_URL)


@pytest.fixture
def private_service(db_session):
    return VersionService(db_session, public_read=False, public_url=PUBLIC_URL)


@pytest.fixture(params=[True, False], ids=["public-read", "private-read"])
def any_service(request, db_session):
    return VersionService(db_session, public_read=request.param, public_url=PUBLIC_URL)


@pytest.fixture
def skills(db_session):
    return SkillService(db_session, public_read=True)


@pytest.fixture
def packages(db_session, blob_store):
    return PackageService(db_session, blob_store, public_read=True, auto_publish=False)


async def stored_version(session_factory, name: str, version: str):
    async with session_factory() as session:
        skill = await skill_repo.get_skill_by_name(session, name)
        found = await skill_repo.get_version(session, skill.id, version)
        return (
            found.status,
            found.published_by,
            found.published_at,
            found.deprecated_at,
            skill.status,
            skill.latest_version,
        )


RANKED = ["0.9.0", "1.0.0", "1.1.0", "1.1.1", "2.0.0-rc.1", "2.0.0", "2.1.0-beta.1"]
STABLE = frozenset({"0.9.0", "1.0.0", "1.1.0", "1.1.1", "2.0.0"})
RANK = {version: index for index, version in enumerate(RANKED)}
MODEL_ACTIONS = ("upload_draft", "upload_published", "publish", "deprecate", "hide", "restore")


class LifecycleModel:
    def __init__(self):
        self.versions: dict[str, str] = {}
        self.hidden = False

    def latest(self):
        published = [item for item, state in self.versions.items() if state == "published"]
        pool = [item for item in published if item in STABLE] or published
        return max(pool, key=RANK.get) if pool else None

    def source(self):
        return self.latest() or max(self.versions, key=RANK.get)

    def status(self):
        if self.hidden:
            return "unpublished"
        return "published" if self.latest() else "draft"

    def ordered(self, *, public):
        wanted = [item for item in self.versions if not public or self.versions[item] != "draft"]
        return [(item, self.versions[item]) for item in sorted(wanted, key=RANK.get, reverse=True)]


class TestList:
    @pytest.mark.parametrize(
        ("actor", "entry"),
        [(actor, entry) for entry in VISIBLE_TO for actor in ACTOR_NAMES],
    )
    async def test_entry_visibility_matrix(self, service, actors, catalog, actor, entry):
        principal = actors.get(actor)
        if actor in VISIBLE_TO[entry]:
            versions = await service.list(principal, entry)
            assert versions
            assert all(isinstance(item, VersionSummary) for item in versions)
        else:
            with pytest.raises(NotFoundError):
                await service.list(principal, entry)

    @pytest.mark.parametrize("actor", ACTOR_NAMES)
    async def test_versions_are_ordered_by_semver_and_filtered_by_actor(
        self, service, actors, catalog, actor
    ):
        versions = await service.list(actors.get(actor), "pub-skill")
        expected = ["2.0.0-beta.1", "1.1.0", "1.0.0", "0.9.0"]
        if actor in PRIVILEGED_ACTORS:
            expected = ["2.0.0", *expected]
        assert [item.version for item in versions] == expected

    async def test_the_prerelease_sorts_below_its_release(self, service, actors, catalog):
        versions = await service.list(actors.owner, "pub-skill")
        order = [item.version for item in versions]
        assert order.index("2.0.0") < order.index("2.0.0-beta.1")

    async def test_the_listing_by_id_matches_the_listing_by_name(self, service, actors, catalog):
        by_id = await service.list(actors.anonymous, catalog.published_id)
        by_name = await service.list(actors.anonymous, "pub-skill")
        assert by_id == by_name

    async def test_summaries_expose_the_contract_fields(self, service, actors, catalog):
        versions = await service.list(actors.owner, "pub-skill")
        dumped = versions[0].model_dump(mode="json")
        assert set(dumped) == {
            "id",
            "skill_id",
            "version",
            "status",
            "sha256",
            "size_bytes",
            "uncompressed_bytes",
            "file_count",
            "entrypoint",
            "display_name",
            "description",
            "category",
            "tags",
            "uploaded_by",
            "published_by",
            "published_at",
            "deprecated_at",
            "created_at",
        }

    async def test_a_deprecated_version_carries_its_deprecation_date(
        self, service, actors, catalog
    ):
        versions = await service.list(actors.anonymous, "pub-skill")
        deprecated = next(item for item in versions if item.version == "0.9.0")
        assert deprecated.status == "deprecated"
        assert deprecated.deprecated_at is not None
        assert deprecated.deprecated_at.utcoffset().total_seconds() == 0

    async def test_private_registry_requires_a_principal(self, private_service, actors, catalog):
        with pytest.raises(UnauthorizedError):
            await private_service.list(None, "pub-skill")
        assert await private_service.list(actors.stranger, "pub-skill")


class TestGet:
    @pytest.mark.parametrize("actor", ACTOR_NAMES)
    @pytest.mark.parametrize(
        ("version", "public"),
        [("0.9.0", True), ("1.0.0", True), ("2.0.0-beta.1", True), ("2.0.0", False)],
    )
    async def test_exact_version_visibility_matrix(
        self, service, actors, catalog, actor, version, public
    ):
        principal = actors.get(actor)
        if public or actor in PRIVILEGED_ACTORS:
            detail = await service.get(principal, "pub-skill", version)
            assert isinstance(detail, VersionDetail)
            assert detail.version == version
        else:
            with pytest.raises(NotFoundError):
                await service.get(principal, "pub-skill", version)

    @pytest.mark.parametrize("actor", ACTOR_NAMES)
    async def test_latest_is_the_highest_stable_published_version_for_everyone(
        self, service, actors, catalog, actor
    ):
        detail = await service.get(actors.get(actor), "pub-skill", "latest")
        assert detail.version == "1.1.0"
        assert detail.status == "published"

    async def test_latest_is_the_default(self, service, actors, catalog):
        assert (await service.get(None, "pub-skill")).version == "1.1.0"

    async def test_a_draft_is_not_found_rather_than_forbidden_for_strangers(
        self, service, actors, catalog
    ):
        with pytest.raises(NotFoundError):
            await service.get(actors.stranger, "pub-skill", "2.0.0")

    @pytest.mark.parametrize(
        "version", ["9.9.9", "", "latest ", "LATEST", "1.0", "x" * 100, "1\x00"]
    )
    async def test_unknown_or_malformed_versions_are_not_found(
        self, service, actors, catalog, version
    ):
        with pytest.raises(NotFoundError):
            await service.get(actors.admin, "pub-skill", version)

    @pytest.mark.parametrize("version", ["1\x00", "x" * 51, "", 7, None])
    async def test_unusable_versions_never_reach_the_database(
        self, service, actors, catalog, monkeypatch, version
    ):
        async def forbidden(*args, **kwargs):
            raise AssertionError("the database must not be queried")

        monkeypatch.setattr(skill_repo, "get_version", forbidden)

        with pytest.raises(NotFoundError):
            await service.get(actors.admin, "pub-skill", version)

    async def test_a_non_string_version_is_not_found(self, service, actors, catalog):
        with pytest.raises(NotFoundError):
            await service.get(actors.admin, "pub-skill", None)

    async def test_latest_of_an_entry_without_published_versions_is_not_found(
        self, service, actors, catalog
    ):
        with pytest.raises(NotFoundError):
            await service.get(actors.owner, "draft-skill", "latest")

    async def test_latest_still_resolves_for_the_owner_of_a_hidden_entry(
        self, service, actors, catalog
    ):
        detail = await service.get(actors.owner, "hidden-ground", "latest")
        assert detail.version == "1.0.0"
        with pytest.raises(NotFoundError):
            await service.get(actors.stranger, "hidden-ground", "latest")

    async def test_the_detail_extends_the_summary_with_metadata_and_download_url(
        self, service, actors, catalog
    ):
        detail = await service.get(actors.anonymous, "pub-skill", "latest")
        summary = next(
            item
            for item in await service.list(actors.anonymous, "pub-skill")
            if item.version == "1.1.0"
        )
        assert detail.model_dump(exclude={"metadata", "download_url"}) == summary.model_dump()
        assert detail.metadata == {}
        assert detail.download_url == (
            f"{PUBLIC_URL}/api/v1/skills/pub-skill/versions/1.1.0/download"
        )

    async def test_the_download_url_names_the_resolved_version_not_latest(
        self, service, actors, catalog
    ):
        detail = await service.get(None, "pub-skill", "latest")
        assert "latest" not in detail.download_url
        assert detail.download_url.endswith("/versions/1.1.0/download")

    async def test_the_download_url_never_has_a_double_slash(self, db_session, actors, catalog):
        service = VersionService(db_session, public_url="https://example.org///")
        detail = await service.get(None, "pub-skill", "1.0.0")
        assert detail.download_url == (
            "https://example.org/api/v1/skills/pub-skill/versions/1.0.0/download"
        )

    async def test_the_public_url_is_stored_without_trailing_slashes(self, db_session):
        assert VersionService(db_session, public_url="https://example.org///").public_url == (
            "https://example.org"
        )
        assert VersionService(db_session, public_url="https://example.org").public_url == (
            "https://example.org"
        )

    async def test_the_public_url_defaults_to_the_settings(
        self, db_session, actors, catalog, monkeypatch
    ):
        monkeypatch.setattr(settings.server, "public_url", "https://configured.example")
        detail = await VersionService(db_session).get(None, "pub-skill", "1.0.0")
        assert detail.download_url.startswith("https://configured.example/api/v1/")

    async def test_stored_metadata_is_returned_as_an_object(self, service, db_session, actors):
        skill_id = await add_skill(db_session, actors.owner, "meta-skill")
        await add_version(
            db_session,
            skill_id,
            "1.0.0",
            actors.owner,
            metadata={"author": "ada", "nested": {"keys": [1, 2]}},
        )
        detail = await service.get(None, "meta-skill", "1.0.0")
        assert detail.metadata == {"author": "ada", "nested": {"keys": [1, 2]}}

    async def test_private_registry_requires_a_principal(self, private_service, actors, catalog):
        with pytest.raises(UnauthorizedError):
            await private_service.get(None, "pub-skill", "1.0.0")
        assert (await private_service.get(actors.stranger, "pub-skill", "1.0.0")).version == "1.0.0"

    async def test_the_private_registry_default_comes_from_the_settings(
        self, db_session, actors, catalog, monkeypatch
    ):
        monkeypatch.setattr(settings.registry, "public_read", False)
        with pytest.raises(UnauthorizedError):
            await VersionService(db_session).list(None, "pub-skill")


class TestDownloadUrl:
    def test_the_url_is_absolute_and_names_the_version(self):
        url = mappers.download_url("https://registry.example.com", "pub-skill", "1.2.3-rc.1")
        assert (
            url
            == "https://registry.example.com/api/v1/skills/pub-skill/versions/1.2.3-rc.1/download"
        )

    def test_trailing_slashes_in_the_base_are_dropped(self):
        url = mappers.download_url("https://registry.example.com//", "a", "1.0.0")
        assert url == "https://registry.example.com/api/v1/skills/a/versions/1.0.0/download"

    @pytest.mark.parametrize(
        ("name", "version", "encoded"),
        [
            ("a/b", "1.0.0", "a%2Fb/versions/1.0.0"),
            ("a b", "1.0.0", "a%20b/versions/1.0.0"),
            ("a?x=1", "1.0.0", "a%3Fx%3D1/versions/1.0.0"),
            ("a#frag", "1.0.0", "a%23frag/versions/1.0.0"),
            ("..", "1.0.0", "../versions/1.0.0"),
            ("ok", "1.0.0/../../x", "ok/versions/1.0.0%2F..%2F..%2Fx"),
            ("ok", "1.0.0?x", "ok/versions/1.0.0%3Fx"),
        ],
    )
    def test_hostile_characters_are_percent_encoded_not_interpreted(self, name, version, encoded):
        url = mappers.download_url("https://r.example", name, version)
        assert url == f"https://r.example/api/v1/skills/{encoded}/download"
        assert "?" not in url and "#" not in url and " " not in url


class TestLatestResolution:
    async def test_only_prereleases_published_resolve_to_the_highest_one(
        self, service, db_session, actors
    ):
        skill_id = await add_skill(
            db_session, actors.owner, "pre-only", latest_version="1.0.0-rc.1"
        )
        for number in ("1.0.0-alpha.1", "1.0.0-rc.1", "1.0.0-beta.1"):
            await add_version(db_session, skill_id, number, actors.owner)
        detail = await service.get(None, "pre-only", "latest")
        assert detail.version == "1.0.0-rc.1"

    async def test_a_stable_release_beats_a_higher_prerelease(self, service, db_session, actors):
        skill_id = await add_skill(db_session, actors.owner, "mixed")
        await add_version(db_session, skill_id, "1.0.0", actors.owner)
        await add_version(db_session, skill_id, "2.0.0-rc.1", actors.owner)
        assert (await service.get(None, "mixed", "latest")).version == "1.0.0"

    async def test_deprecated_and_draft_versions_are_never_latest(
        self, service, db_session, actors
    ):
        skill_id = await add_skill(db_session, actors.owner, "never-latest")
        await add_version(db_session, skill_id, "1.0.0", actors.owner)
        await add_version(
            db_session, skill_id, "2.0.0", actors.owner, status=VersionStatus.DEPRECATED
        )
        await add_version(db_session, skill_id, "3.0.0", actors.owner, status=VersionStatus.DRAFT)
        assert (await service.get(actors.owner, "never-latest", "latest")).version == "1.0.0"

    async def test_semver_precedence_is_numeric_not_lexicographic(
        self, service, db_session, actors
    ):
        skill_id = await add_skill(db_session, actors.owner, "numeric")
        for number in ("1.9.0", "1.10.0", "1.2.0"):
            await add_version(db_session, skill_id, number, actors.owner)
        assert (await service.get(None, "numeric", "latest")).version == "1.10.0"
        listing = await service.list(None, "numeric")
        assert [item.version for item in listing] == ["1.10.0", "1.9.0", "1.2.0"]


class TestPublish:
    @pytest.mark.parametrize(("actor", "entry", "outcome"), WRITE_CASES, ids=WRITE_IDS)
    async def test_authorization_matrix(self, any_service, actors, catalog, actor, entry, outcome):
        service = any_service
        principal = actors.get(actor)
        version = SEEDED_VERSIONS[entry]
        if outcome is None:
            summary = await service.publish(principal, entry, version)
            assert isinstance(summary, VersionSummary)
            assert (summary.version, summary.status) == (version, "published")
        else:
            with pytest.raises(outcome):
                await service.publish(principal, entry, version)

    async def test_non_owners_get_the_same_answer_for_real_and_imaginary_drafts(
        self, service, actors, catalog
    ):
        with pytest.raises(ForbiddenError) as real:
            await service.publish(actors.stranger, "pub-skill", "2.0.0")
        with pytest.raises(ForbiddenError) as imaginary:
            await service.publish(actors.stranger, "pub-skill", "9.9.9")
        assert real.value.message == imaginary.value.message

    async def test_read_scope_is_rejected_before_anything_is_looked_up(
        self, service, actors, catalog
    ):
        for ref, version in (("pub-skill", "2.0.0"), ("no-such", "1.0.0"), ("pub-skill", "9.9.9")):
            with pytest.raises(ForbiddenError):
                await service.publish(actors.owner_read, ref, version)

    async def test_publishing_a_draft_records_who_and_when(
        self, service, actors, catalog, session_factory
    ):
        summary = await service.publish(actors.owner, "pub-skill", "2.0.0")

        assert summary.status == "published"
        assert summary.published_by == actors.owner.user_id
        assert summary.published_at is not None
        assert summary.deprecated_at is None
        status, by, at, deprecated_at, entry_status, latest = await stored_version(
            session_factory, "pub-skill", "2.0.0"
        )
        assert (status, by, deprecated_at) == (
            VersionStatus.PUBLISHED,
            actors.owner.user_id,
            None,
        )
        assert at is not None
        assert (entry_status, latest) == (SkillStatus.PUBLISHED, "2.0.0")

    async def test_an_admin_publishing_a_draft_is_recorded_as_the_publisher(
        self, service, actors, catalog
    ):
        summary = await service.publish(actors.admin, "pub-skill", "2.0.0")
        assert summary.published_by == actors.admin.user_id
        assert summary.uploaded_by == actors.owner.user_id

    async def test_publishing_updates_latest_for_every_reader(self, service, actors, catalog):
        await service.publish(actors.owner, "pub-skill", "2.0.0")
        assert (await service.get(None, "pub-skill", "latest")).version == "2.0.0"

    async def test_publishing_a_prerelease_leaves_a_stable_latest_untouched(
        self, service, db_session, actors
    ):
        skill_id = await add_skill(db_session, actors.owner, "keep-stable")
        await add_version(db_session, skill_id, "1.0.0", actors.owner)
        await add_version(
            db_session, skill_id, "2.0.0-rc.1", actors.owner, status=VersionStatus.DRAFT
        )

        await service.publish(actors.owner, "keep-stable", "2.0.0-rc.1")

        assert (await service.get(None, "keep-stable", "latest")).version == "1.0.0"
        assert (await SkillService(db_session).get(None, "keep-stable")).latest_version == "1.0.0"

    async def test_publishing_the_first_version_publishes_the_entry(
        self, service, skills, actors, catalog
    ):
        assert (await skills.get(actors.owner, "draft-skill")).status == "draft"

        await service.publish(actors.owner, "draft-skill", "0.1.0")

        detail = await skills.get(None, "draft-skill")
        assert (detail.status, detail.latest_version) == ("published", "0.1.0")

    async def test_publishing_is_idempotent_and_keeps_the_original_publisher(
        self, service, actors, catalog
    ):
        first = await service.publish(actors.owner, "pub-skill", "2.0.0")
        again = await service.publish(actors.admin, "pub-skill", "2.0.0")
        assert again == first
        assert again.published_by == actors.owner.user_id

    async def test_publishing_an_already_published_version_writes_nothing(
        self, service, engine, actors, catalog
    ):
        with record_writes(engine) as writes:
            await service.publish(actors.owner, "pub-skill", "1.0.0")
        assert writes == []

        with record_writes(engine) as writes:
            await service.publish(actors.owner, "pub-skill", "2.0.0")
        assert any(statement.startswith("UPDATE skill_versions") for statement in writes)

    async def test_publishing_an_already_published_version_changes_nothing(
        self, service, actors, catalog
    ):
        before = await service.get(None, "pub-skill", "1.0.0")
        after = await service.publish(actors.admin, "pub-skill", "1.0.0")
        assert after.model_dump() == before.model_dump(exclude={"metadata", "download_url"})

    async def test_publishing_a_deprecated_version_undoes_the_deprecation(
        self, service, actors, catalog
    ):
        before = next(
            item for item in await service.list(None, "pub-skill") if item.version == "0.9.0"
        )
        assert before.status == "deprecated"

        restored = await service.publish(actors.owner, "pub-skill", "0.9.0")

        assert restored.status == "published"
        assert restored.deprecated_at is None
        assert restored.published_at == before.published_at
        assert restored.published_by == before.published_by

    @pytest.mark.parametrize("version", ["9.9.9", "latest", "", "2.0", "x" * 80])
    async def test_publishing_an_unknown_version_is_not_found(
        self, service, actors, catalog, version
    ):
        with pytest.raises(NotFoundError):
            await service.publish(actors.owner, "pub-skill", version)

    async def test_publishing_in_a_missing_entry_is_not_found(self, service, actors, catalog):
        with pytest.raises(NotFoundError):
            await service.publish(actors.owner, "no-such", "1.0.0")

    async def test_publishing_by_entry_id_works(self, service, actors, catalog):
        summary = await service.publish(actors.owner, catalog.published_id, "2.0.0")
        assert summary.skill_id == catalog.published_id

    async def test_a_failure_while_recalculating_rolls_the_publication_back(
        self, service, db_session, actors, catalog, session_factory, monkeypatch
    ):
        async def broken(*args, **kwargs):
            raise RuntimeError("recalculation failed")

        monkeypatch.setattr(catalog_module, "refresh_skill", broken)
        with pytest.raises(RuntimeError, match="recalculation failed"):
            await service.publish(actors.owner, "pub-skill", "2.0.0")
        monkeypatch.undo()

        status, by, at, _, entry_status, latest = await stored_version(
            session_factory, "pub-skill", "2.0.0"
        )
        assert (status, by, at) == (VersionStatus.DRAFT, None, None)
        assert (entry_status, latest) == (SkillStatus.PUBLISHED, "1.1.0")
        again = await service.publish(actors.owner, "pub-skill", "2.0.0")
        assert again.status == "published"

    async def test_publication_is_persisted_for_other_sessions(
        self, service, actors, catalog, session_factory
    ):
        await service.publish(actors.owner, "pub-skill", "2.0.0")
        async with session_factory() as other:
            detail = await VersionService(other, public_read=True).get(None, "pub-skill", "2.0.0")
        assert detail.status == "published"


class TestDeprecate:
    @pytest.mark.parametrize(("actor", "entry", "outcome"), DEPRECATE_CASES)
    async def test_authorization_and_state_matrix(
        self, any_service, actors, catalog, actor, entry, outcome
    ):
        service = any_service
        principal = actors.get(actor)
        version = SEEDED_DEPRECABLE[entry]
        if outcome is None:
            summary = await service.deprecate(principal, entry, version)
            assert (summary.version, summary.status) == (version, "deprecated")
        else:
            with pytest.raises(outcome):
                await service.deprecate(principal, entry, version)

    async def test_non_owners_cannot_probe_for_draft_versions(self, service, actors, catalog):
        with pytest.raises(ForbiddenError) as real:
            await service.deprecate(actors.stranger, "pub-skill", "2.0.0")
        with pytest.raises(ForbiddenError) as imaginary:
            await service.deprecate(actors.stranger, "pub-skill", "9.9.9")
        assert real.value.message == imaginary.value.message

    async def test_deprecating_records_the_date_and_keeps_the_version_resolvable(
        self, service, actors, catalog, session_factory
    ):
        summary = await service.deprecate(actors.owner, "pub-skill", "1.0.0")

        assert summary.status == "deprecated"
        assert summary.deprecated_at is not None
        assert summary.published_at is not None
        fetched = await service.get(None, "pub-skill", "1.0.0")
        assert fetched.status == "deprecated"
        stored = await stored_version(session_factory, "pub-skill", "1.0.0")
        assert stored[0] == VersionStatus.DEPRECATED and stored[3] is not None

    async def test_deprecating_a_non_latest_version_keeps_latest(self, service, actors, catalog):
        await service.deprecate(actors.owner, "pub-skill", "1.0.0")
        assert (await service.get(None, "pub-skill", "latest")).version == "1.1.0"

    async def test_deprecating_latest_moves_latest_to_the_previous_stable_release(
        self, service, skills, actors, catalog
    ):
        await service.deprecate(actors.owner, "pub-skill", "1.1.0")

        assert (await service.get(None, "pub-skill", "latest")).version == "1.0.0"
        assert (await skills.get(None, "pub-skill")).latest_version == "1.0.0"

    async def test_deprecating_the_last_stable_release_falls_back_to_the_prerelease(
        self, service, skills, actors, catalog
    ):
        await service.deprecate(actors.owner, "pub-skill", "1.1.0")
        await service.deprecate(actors.owner, "pub-skill", "1.0.0")

        assert (await service.get(None, "pub-skill", "latest")).version == "2.0.0-beta.1"
        assert (await skills.get(None, "pub-skill")).latest_version == "2.0.0-beta.1"

    async def test_the_only_published_version_cannot_be_deprecated(self, service, actors, catalog):
        with pytest.raises(ConflictError, match="only published version"):
            await service.deprecate(actors.owner, "hidden-ground", "1.0.0")
        detail = await service.get(actors.owner, "hidden-ground", "1.0.0")
        assert detail.status == "published"

    async def test_the_last_remaining_published_version_cannot_be_deprecated_either(
        self, service, actors, catalog
    ):
        await service.deprecate(actors.owner, "pub-skill", "1.1.0")
        await service.deprecate(actors.owner, "pub-skill", "1.0.0")
        with pytest.raises(ConflictError):
            await service.deprecate(actors.owner, "pub-skill", "2.0.0-beta.1")
        assert (await service.get(None, "pub-skill", "latest")).version == "2.0.0-beta.1"

    async def test_a_draft_version_cannot_be_deprecated(self, service, actors, catalog):
        with pytest.raises(ConflictError, match="published"):
            await service.deprecate(actors.owner, "pub-skill", "2.0.0")
        draft = await service.get(actors.owner, "pub-skill", "2.0.0")
        assert draft.status == "draft"

    async def test_deprecating_an_already_deprecated_version_writes_nothing(
        self, service, engine, actors, catalog
    ):
        with record_writes(engine) as writes:
            await service.deprecate(actors.owner, "pub-skill", "0.9.0")
        assert writes == []

    async def test_deprecating_twice_is_idempotent(self, service, actors, catalog):
        first = await service.deprecate(actors.owner, "pub-skill", "0.9.0")
        again = await service.deprecate(actors.admin, "pub-skill", "0.9.0")
        assert again == first
        assert again.status == "deprecated"

    @pytest.mark.parametrize("version", ["9.9.9", "latest", ""])
    async def test_an_unknown_version_is_not_found(self, service, actors, catalog, version):
        with pytest.raises(NotFoundError):
            await service.deprecate(actors.owner, "pub-skill", version)

    async def test_a_failure_while_recalculating_rolls_the_deprecation_back(
        self, service, actors, catalog, session_factory, monkeypatch
    ):
        async def broken(*args, **kwargs):
            raise RuntimeError("recalculation failed")

        monkeypatch.setattr(catalog_module, "refresh_skill", broken)
        with pytest.raises(RuntimeError):
            await service.deprecate(actors.owner, "pub-skill", "1.1.0")
        monkeypatch.undo()

        status, _, _, deprecated_at, _, latest = await stored_version(
            session_factory, "pub-skill", "1.1.0"
        )
        assert (status, deprecated_at, latest) == (VersionStatus.PUBLISHED, None, "1.1.0")

    async def test_republishing_a_deprecated_version_makes_it_latest_again(
        self, service, actors, catalog
    ):
        await service.deprecate(actors.owner, "pub-skill", "1.1.0")
        await service.publish(actors.owner, "pub-skill", "1.1.0")
        assert (await service.get(None, "pub-skill", "latest")).version == "1.1.0"


class TestLifecycle:
    @pytest.fixture
    def owner(self, actors):
        return actors.owner

    async def upload(self, packages, owner, version, **options):
        return await upload_package(packages, owner, "life-skill", version, **options)

    async def test_a_full_life_with_latest_denormalization_prereleases_and_hiding(
        self, packages, service, skills, owner, actors
    ):
        name = "life-skill"

        first = await self.upload(
            packages,
            owner,
            "1.0.0",
            publish=False,
            description="first",
            tags=["a"],
            category="cat-a",
        )
        assert first.version.status == "draft"
        assert first.skill.status == "draft"
        assert first.skill.latest_version is None
        assert (first.skill.description, first.skill.tags, first.skill.category) == (
            "first",
            ["a"],
            "cat-a",
        )
        with pytest.raises(NotFoundError):
            await skills.get(None, name)
        with pytest.raises(NotFoundError):
            await service.get(owner, name, "latest")

        published = await service.publish(owner, name, "1.0.0")
        detail = await skills.get(None, name)
        assert published.status == "published"
        assert (detail.status, detail.latest_version) == ("published", "1.0.0")

        draft = await self.upload(
            packages,
            owner,
            "1.1.0",
            publish=False,
            description="second",
            tags=["b"],
            category="cat-b",
        )
        assert draft.skill.latest_version == "1.0.0"
        assert (draft.skill.description, draft.skill.tags) == ("first", ["a"])

        await service.publish(owner, name, "1.1.0")
        detail = await skills.get(None, name)
        assert (detail.latest_version, detail.description) == ("1.1.0", "second")
        assert (detail.tags, detail.category) == (["b"], "cat-b")
        assert (await skills.search(None, tags=["a"])).total == 0
        assert [item.name for item in (await skills.search(None, tags=["b"])).items] == [name]
        assert [item.name for item in (await skills.search(None, category="cat-b")).items] == [name]

        await self.upload(
            packages, owner, "2.0.0-rc.1", publish=True, description="rc", tags=["rc"]
        )
        assert (await skills.get(None, name)).latest_version == "1.1.0"
        assert (await skills.get(None, name)).description == "second"

        third = await self.upload(
            packages, owner, "2.0.0", publish=True, description="third", tags=["c"]
        )
        assert third.skill.latest_version == "2.0.0"
        detail = await skills.get(None, name)
        assert (detail.latest_version, detail.description, detail.tags) == ("2.0.0", "third", ["c"])

        await service.deprecate(owner, name, "2.0.0")
        detail = await skills.get(None, name)
        assert (detail.latest_version, detail.description) == ("1.1.0", "second")
        deprecated = await service.get(None, name, "2.0.0")
        assert deprecated.status == "deprecated"
        assert (await service.get(None, name, "latest")).version == "1.1.0"

        await service.deprecate(owner, name, "1.1.0")
        detail = await skills.get(None, name)
        assert (detail.latest_version, detail.description) == ("1.0.0", "first")

        await service.deprecate(owner, name, "1.0.0")
        detail = await skills.get(None, name)
        assert (detail.latest_version, detail.description) == ("2.0.0-rc.1", "rc")

        with pytest.raises(ConflictError):
            await service.deprecate(owner, name, "2.0.0-rc.1")
        assert (await skills.get(None, name)).latest_version == "2.0.0-rc.1"

        original = next(item for item in await service.list(None, name) if item.version == "2.0.0")
        revived = await service.publish(owner, name, "2.0.0")
        assert revived.status == "published" and revived.deprecated_at is None
        assert revived.published_at == original.published_at
        assert (await skills.get(None, name)).latest_version == "2.0.0"

        await skills.hide(owner, name)
        hidden_upload = await self.upload(packages, owner, "3.0.0", publish=True, description="v3")
        assert hidden_upload.skill.status == "unpublished"
        assert hidden_upload.skill.latest_version == "3.0.0"
        with pytest.raises(NotFoundError):
            await skills.get(None, name)
        with pytest.raises(NotFoundError):
            await service.get(None, name, "latest")
        assert (await skills.get(owner, name)).status == "unpublished"

        restored = await skills.restore(owner, name)
        assert (restored.status, restored.latest_version) == ("published", "3.0.0")
        assert (await skills.get(None, name)).description == "v3"
        assert [item.version for item in restored.versions] == [
            "3.0.0",
            "2.0.0",
            "2.0.0-rc.1",
            "1.1.0",
            "1.0.0",
        ]

    async def test_publishing_a_backport_changes_neither_latest_nor_the_description(
        self, packages, service, skills, owner
    ):
        name = "life-skill"
        await self.upload(packages, owner, "2.0.0", publish=True, description="current")

        backport = await self.upload(
            packages, owner, "1.5.0", publish=True, description="backport", tags=["old"]
        )

        assert backport.skill.latest_version == "2.0.0"
        assert backport.skill.description == "current"
        detail = await skills.get(None, name)
        assert (detail.latest_version, detail.description, detail.tags) == ("2.0.0", "current", [])
        assert [item.version for item in detail.versions] == ["2.0.0", "1.5.0"]
        assert (await skills.search(None, tags=["old"])).total == 0

    async def test_the_entry_drops_tags_and_category_the_new_latest_no_longer_has(
        self, packages, service, skills, owner
    ):
        name = "life-skill"
        await self.upload(packages, owner, "1.0.0", publish=True, tags=["a", "b"], category="cat-a")
        assert (await skills.get(None, name)).tags == ["a", "b"]

        await self.upload(packages, owner, "1.1.0", publish=True)

        detail = await skills.get(None, name)
        assert (detail.tags, detail.category) == ([], None)
        assert (await skills.search(None, tags=["a"])).total == 0
        assert (await skills.search(None, category="cat-a")).total == 0
        assert await skills.tags(None) == []
        assert await skills.categories(None) == []

    async def test_deprecating_and_publishing_keep_a_hidden_entry_hidden(
        self, packages, service, skills, owner
    ):
        name = "life-skill"
        await self.upload(packages, owner, "1.0.0", publish=True)
        await self.upload(packages, owner, "1.1.0", publish=True)
        await skills.hide(owner, name)

        await service.deprecate(owner, name, "1.1.0")
        assert (await skills.get(owner, name)).status == "unpublished"
        await service.publish(owner, name, "1.1.0")
        assert (await skills.get(owner, name)).status == "unpublished"
        with pytest.raises(NotFoundError):
            await skills.get(None, name)

    async def test_an_entry_with_only_drafts_describes_itself_with_its_highest_version(
        self, packages, skills, owner
    ):
        name = "life-skill"
        await self.upload(packages, owner, "0.1.0", publish=False, description="d1")
        await self.upload(packages, owner, "0.2.0", publish=False, description="d2")
        await self.upload(packages, owner, "0.1.5", publish=False, description="d3")

        detail = await skills.get(owner, name)

        assert (detail.status, detail.latest_version) == ("draft", None)
        assert detail.description == "d2"
        assert [item.version for item in detail.versions] == ["0.2.0", "0.1.5", "0.1.0"]

    async def test_publishing_a_lower_draft_makes_the_entry_describe_that_version(
        self, packages, service, skills, owner
    ):
        name = "life-skill"
        await self.upload(packages, owner, "0.1.0", publish=False, description="d1")
        await self.upload(packages, owner, "0.2.0", publish=False, description="d2")

        await service.publish(owner, name, "0.1.0")

        detail = await skills.get(owner, name)
        assert (detail.status, detail.latest_version, detail.description) == (
            "published",
            "0.1.0",
            "d1",
        )

    async def test_restoring_an_entry_that_never_had_a_publication_leaves_it_as_draft(
        self, packages, skills, owner
    ):
        await self.upload(packages, owner, "0.1.0", publish=False)
        await skills.hide(owner, "life-skill")
        detail = await skills.restore(owner, "life-skill")
        assert (detail.status, detail.latest_version) == ("draft", None)

    async def test_lifecycle_events_are_logged(self, packages, service, owner, caplog):
        await self.upload(packages, owner, "1.0.0", publish=True)
        await self.upload(packages, owner, "1.1.0", publish=False)
        with caplog.at_level(logging.INFO, logger="app.services.version"):
            await service.publish(owner, "life-skill", "1.1.0")
            await service.deprecate(owner, "life-skill", "1.0.0")
            with pytest.raises(ConflictError):
                await service.deprecate(owner, "life-skill", "1.1.0")

        messages = [record.getMessage() for record in caplog.records]
        assert "Version published: life-skill 1.1.0" in messages
        assert "Version deprecated: life-skill 1.0.0" in messages
        assert any(message.startswith("Deprecation rejected") for message in messages)
        rejected = next(r for r in caplog.records if r.getMessage().startswith("Deprecation rej"))
        assert rejected.levelno == logging.WARNING


@pytest_asyncio.fixture
async def file_sessions(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'versions.db'}", connect_args={"timeout": 60}
    )
    configure_sqlite_engine(engine)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


class TestConcurrentDeprecation:
    @staticmethod
    async def seed(file_sessions, rounds, versions):
        async with file_sessions() as session:
            owner = principal_of(await make_user(session, "racer"))
            names = []
            for round_number in range(rounds):
                name = f"race-{round_number}"
                skill_id = await add_skill(
                    session, owner, name, latest_version=versions[-1], category="ops"
                )
                for number in versions:
                    await add_version(session, skill_id, number, owner, category="ops")
                names.append(name)
        return owner, names

    @staticmethod
    async def deprecate(file_sessions, owner, name, version):
        async with file_sessions() as session:
            service = VersionService(session, public_read=True, public_url=PUBLIC_URL)
            return await service.deprecate(owner, name, version)

    @staticmethod
    async def stored(file_sessions, name):
        async with file_sessions() as session:
            skill = await skill_repo.get_skill_by_name(session, name)
            versions = await skill_repo.list_versions(session, skill.id)
            return skill, {item.version: item.status for item in versions}

    @pytest.mark.parametrize(
        "versions", [("1.0.0", "1.1.0"), ("1.0.0", "1.1.0", "1.2.0")], ids=["two", "three"]
    )
    async def test_concurrent_deprecations_never_leave_an_entry_without_a_published_version(
        self, file_sessions, versions
    ):
        owner, names = await self.seed(file_sessions, 8, versions)

        for index, name in enumerate(names):
            order = versions if index % 2 == 0 else tuple(reversed(versions))
            outcomes = await asyncio.gather(
                *(self.deprecate(file_sessions, owner, name, number) for number in order),
                return_exceptions=True,
            )

            conflicts = [item for item in outcomes if isinstance(item, ConflictError)]
            done = [item for item in outcomes if isinstance(item, VersionSummary)]
            assert len(conflicts) == 1 and len(done) == len(versions) - 1, outcomes
            skill, states = await self.stored(file_sessions, name)
            alive = [
                number for number, status in states.items() if status == VersionStatus.PUBLISHED
            ]
            assert len(alive) == 1, states
            assert skill.status == SkillStatus.PUBLISHED
            assert skill.latest_version == alive[0]

    async def test_the_entry_stays_consistent_for_readers_after_the_race(self, file_sessions):
        owner, names = await self.seed(file_sessions, 4, ("1.0.0", "1.1.0"))

        for name in names:
            await asyncio.gather(
                self.deprecate(file_sessions, owner, name, "1.0.0"),
                self.deprecate(file_sessions, owner, name, "1.1.0"),
                return_exceptions=True,
            )

        async with file_sessions() as session:
            skills = SkillService(session, public_read=True)
            versions = VersionService(session, public_read=True, public_url=PUBLIC_URL)
            for name in names:
                detail = await skills.get(None, name)
                latest = await versions.get(None, name, "latest")
                assert detail.status == "published"
                assert detail.latest_version == latest.version
                assert latest.status == "published"
            page = await skills.search(None, category="ops", limit=100)
            assert page.total == len(names)

    async def test_a_lost_race_rolls_everything_back(self, file_sessions):
        owner, (name,) = await self.seed(file_sessions, 1, ("1.0.0", "1.1.0"))
        await self.deprecate(file_sessions, owner, name, "1.0.0")

        with pytest.raises(ConflictError):
            await self.deprecate(file_sessions, owner, name, "1.1.0")

        skill, states = await self.stored(file_sessions, name)
        assert states == {"1.0.0": VersionStatus.DEPRECATED, "1.1.0": VersionStatus.PUBLISHED}
        assert skill.latest_version == "1.1.0"

    async def test_a_stale_sibling_in_the_session_does_not_hide_a_concurrent_deprecation(
        self, file_sessions
    ):
        owner, (name,) = await self.seed(file_sessions, 1, ("1.0.0", "1.1.0"))
        async with file_sessions() as session:
            service = VersionService(session, public_read=True, public_url=PUBLIC_URL)
            skill = await skill_repo.get_skill_by_name(session, name)
            await skill_repo.list_versions(session, skill.id)
            await self.deprecate(file_sessions, owner, name, "1.0.0")

            with pytest.raises(ConflictError):
                await service.deprecate(owner, name, "1.1.0")


class TestEntryLocking:
    @staticmethod
    def trace(engine, db_session):
        events = []

        @event.listens_for(db_session.sync_session, "do_orm_execute")
        def orm(state):
            events.append(("orm", str(state.statement.compile(dialect=postgresql.dialect()))))

        def sql(connection, cursor, statement, parameters, context, executemany):
            events.append(("sql", statement))

        event.listen(engine.sync_engine, "before_cursor_execute", sql)
        return events, lambda: event.remove(engine.sync_engine, "before_cursor_execute", sql)

    @pytest.mark.parametrize("operation", ["publish", "deprecate", "restore", "upload"])
    async def test_every_writer_that_recomputes_the_entry_locks_it_before_writing(
        self, service, skills, packages, engine, db_session, actors, catalog, operation
    ):
        owner = actors.owner
        calls = {
            "publish": lambda: service.publish(owner, "pub-skill", "2.0.0"),
            "deprecate": lambda: service.deprecate(owner, "pub-skill", "1.0.0"),
            "restore": lambda: skills.restore(owner, "hidden-ground"),
            "upload": lambda: upload_package(packages, owner, "pub-skill", "3.0.0"),
        }
        events, stop = self.trace(engine, db_session)
        try:
            await calls[operation]()
        finally:
            stop()

        locks = [
            index
            for index, (kind, text) in enumerate(events)
            if kind == "orm" and "FOR UPDATE" in text
        ]
        writes = [
            index
            for index, (kind, text) in enumerate(events)
            if kind == "sql" and text.lstrip().split(None, 1)[0].upper() in {"INSERT", "UPDATE"}
        ]
        assert len(locks) == 1
        assert "FROM skills" in events[locks[0]][1]
        assert writes and locks[0] < writes[0]

    async def test_a_deprecation_checks_the_remaining_versions_after_the_lock_and_its_write(
        self, service, engine, db_session, actors, catalog
    ):
        events, stop = self.trace(engine, db_session)
        try:
            await service.deprecate(actors.owner, "pub-skill", "1.0.0")
        finally:
            stop()

        orm = [(i, text) for i, (kind, text) in enumerate(events) if kind == "orm"]
        lock = next(i for i, text in orm if "FOR UPDATE" in text)
        update = next(
            i
            for i, (kind, text) in enumerate(events)
            if kind == "sql" and text.startswith("UPDATE")
        )
        guard = next(
            i
            for i, text in orm
            if "FROM skill_versions" in text and "skill_versions.status IN" in text
        )
        assert lock < update < guard

    async def test_requests_that_change_nothing_never_take_the_lock(
        self, service, engine, db_session, actors, catalog
    ):
        events, stop = self.trace(engine, db_session)
        try:
            await service.deprecate(actors.owner, "pub-skill", "0.9.0")
            await service.publish(actors.owner, "pub-skill", "1.0.0")
            with pytest.raises(ConflictError):
                await service.deprecate(actors.owner, "pub-skill", "2.0.0")
        finally:
            stop()

        assert not any("FOR UPDATE" in text for _, text in events)


class TestLifecycleAgainstAModel:
    @pytest.mark.parametrize("seed", range(10))
    async def test_random_operation_sequences_match_an_independent_model(
        self, packages, service, skills, actors, seed
    ):
        owner, name = actors.owner, "model-skill"
        generator = random.Random(seed)
        model = LifecycleModel()

        for step in range(22):
            action = generator.choice(MODEL_ACTIONS)
            version = generator.choice(RANKED)
            context = f"seed={seed} step={step} {action} {version}"
            created = bool(model.versions)

            if action in ("upload_draft", "upload_published"):
                publish = action == "upload_published"
                attempt = upload_package(
                    packages,
                    owner,
                    name,
                    version,
                    publish=publish,
                    description=f"desc-{version}",
                    tags=[f"t{RANK[version]}"],
                )
                if version in model.versions:
                    with pytest.raises(ConflictError):
                        await attempt
                else:
                    await attempt
                    model.versions[version] = "published" if publish else "draft"
            elif action == "publish":
                attempt = service.publish(owner, name, version)
                if version not in model.versions:
                    with pytest.raises(NotFoundError):
                        await attempt
                else:
                    assert (await attempt).status == "published", context
                    model.versions[version] = "published"
            elif action == "deprecate":
                attempt = service.deprecate(owner, name, version)
                state = model.versions.get(version)
                published = [
                    item for item, current in model.versions.items() if current == "published"
                ]
                if state is None:
                    with pytest.raises(NotFoundError):
                        await attempt
                elif state == "draft" or (state == "published" and len(published) == 1):
                    with pytest.raises(ConflictError):
                        await attempt
                else:
                    assert (await attempt).status == "deprecated", context
                    model.versions[version] = "deprecated"
            elif action == "hide":
                if created:
                    await skills.hide(owner, name)
                    model.hidden = True
                else:
                    with pytest.raises(NotFoundError):
                        await skills.hide(owner, name)
            elif created:
                await skills.restore(owner, name)
                model.hidden = False
            else:
                with pytest.raises(NotFoundError):
                    await skills.restore(owner, name)

            if not model.versions:
                continue
            await self.check(skills, service, owner, name, model, context)

    @staticmethod
    async def check(skills, service, owner, name, model, context):
        source = model.source()
        detail = await skills.get(owner, name)
        assert detail.status == model.status(), context
        assert detail.latest_version == model.latest(), context
        assert detail.description == f"desc-{source}", context
        assert detail.tags == [f"t{RANK[source]}"], context
        assert [(v.version, v.status) for v in detail.versions] == model.ordered(public=False), (
            context
        )
        latest = model.latest()
        if latest is None:
            with pytest.raises(NotFoundError):
                await service.get(owner, name, "latest")
        else:
            assert (await service.get(owner, name, "latest")).version == latest, context
        if model.status() == "published":
            public = await skills.get(None, name)
            assert public.latest_version == latest, context
            assert [(v.version, v.status) for v in public.versions] == model.ordered(public=True)
            assert (await skills.search(None, tags=[f"t{RANK[source]}"])).total == 1, context
        else:
            with pytest.raises(NotFoundError):
                await skills.get(None, name)
            assert (await skills.search(None, tags=[f"t{RANK[source]}"])).total == 0, context
