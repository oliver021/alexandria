from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import pytest
import pytest_asyncio
from sqlalchemy import event, inspect, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from app.models import Skill, SkillStatus, SkillVersion, VersionStatus
from app.repositories import skill_repo
from app.services import catalog, mappers, semver
from app.services.errors import NotFoundError
from app.services.package_service import PackageService
from app.services.skill_service import SkillService
from app.services.version_service import VersionService
from tests.helpers.factories import Actors, upload_package

HEAVY_COLUMNS = ("manifest", "metadata", "search_text")
HEAVY_ATTRIBUTES = frozenset({"manifest", "metadata_", "search_text"})
NAME = "fat-skill"
VERSIONS = {
    "1.0.0": True,
    "1.1.0": True,
    "2.0.0-beta.1": True,
    "3.0.0": False,
}
FILES = {f"docs/part-{index:03d}.md": f"part {index}\n" * 3 for index in range(120)}
NOTES = "n" * 3000


def body_of(number: str) -> str:
    return f"Instructions of {number}.\n" * 600


def normalized(statement: str) -> str:
    return " ".join(statement.split())


def selected_columns(statement: str) -> str:
    return normalized(statement).split(" FROM ")[0]


def reads_versions(statement: str) -> bool:
    flat = normalized(statement)
    return flat.startswith("SELECT") and " FROM skill_versions" in flat


def reads_heavy_columns(statement: str) -> bool:
    return reads_versions(statement) and any(
        f"skill_versions.{column}" in selected_columns(statement) for column in HEAVY_COLUMNS
    )


def reads_a_single_row(statement: str) -> bool:
    flat = normalized(statement)
    return "skill_versions.id = ?" in flat or "skill_versions.version = ?" in flat


@dataclass(frozen=True)
class World:
    skills: SkillService
    versions: VersionService
    packages: PackageService
    owner: object


@contextmanager
def recorded(engine: AsyncEngine) -> Iterator[list[str]]:
    statements: list[str] = []

    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)


@pytest_asyncio.fixture
async def world(db_session, blob_store, actors: Actors) -> World:
    packages = PackageService(db_session, blob_store, public_read=True, auto_publish=False)
    for number, published in VERSIONS.items():
        await upload_package(
            packages,
            actors.owner,
            NAME,
            number,
            files=FILES,
            body=body_of(number),
            tags=["fat", f"v{number.replace('.', '-')[:6]}".lower()],
            category="ops",
            extra_frontmatter={"notes": NOTES, "build": number},
            publish=published,
        )
    return World(
        skills=SkillService(db_session, public_read=True),
        versions=VersionService(db_session, public_read=True),
        packages=packages,
        owner=actors.owner,
    )


async def reference_rows(session_factory) -> list[SkillVersion]:
    async with session_factory() as session:
        skill = await skill_repo.get_skill_by_name(session, NAME)
        rows = (
            (await session.execute(select(SkillVersion).where(SkillVersion.skill_id == skill.id)))
            .scalars()
            .all()
        )
        for row in rows:
            assert not HEAVY_ATTRIBUTES & inspect(row).unloaded
        return sorted(rows, key=lambda row: semver.sort_key(row.version), reverse=True)


async def reference_skill(session_factory) -> Skill:
    async with session_factory() as session:
        return await skill_repo.get_skill_by_name(session, NAME)


def expected_source(rows: list[SkillVersion]) -> SkillVersion:
    published = [row for row in rows if row.status == VersionStatus.PUBLISHED]
    latest = semver.pick_latest(row.version for row in published)
    if latest is not None:
        return next(row for row in published if row.version == latest)
    return max(rows, key=lambda row: semver.sort_key(row.version))


class TestReadingVersionsNeverTouchesTheHeavyColumns:
    @pytest.mark.parametrize("viewer", ["anonymous", "owner"])
    async def test_the_entry_detail_selects_no_heavy_column(self, world, engine, actors, viewer):
        with recorded(engine) as statements:
            detail = await world.skills.get(actors.get(viewer), NAME)

        assert len(detail.versions) == (3 if viewer == "anonymous" else 4)
        assert [s for s in statements if reads_versions(s)]
        assert not [s for s in statements if reads_heavy_columns(s)]

    @pytest.mark.parametrize("viewer", ["anonymous", "owner"])
    async def test_the_version_listing_selects_no_heavy_column(self, world, engine, actors, viewer):
        with recorded(engine) as statements:
            listed = await world.versions.list(actors.get(viewer), NAME)

        assert len(listed) == (3 if viewer == "anonymous" else 4)
        assert [s for s in statements if reads_versions(s)]
        assert not [s for s in statements if reads_heavy_columns(s)]

    async def test_the_listed_versions_are_not_loaded_with_their_heavy_attributes(
        self, world, db_session
    ):
        skill = await skill_repo.get_skill_by_name(db_session, NAME)

        listed = await catalog.list_visible_versions(db_session, world.owner, skill)

        assert len(listed) == 4
        for version in listed:
            assert HEAVY_ATTRIBUTES <= inspect(version).unloaded


class TestTheDtosAreUnchanged:
    async def test_the_entry_detail_equals_the_one_built_from_fully_loaded_rows(
        self, world, session_factory, actors
    ):
        rows = await reference_rows(session_factory)
        skill = await reference_skill(session_factory)

        detail = await world.skills.get(actors.owner, NAME)

        assert detail == mappers.skill_detail(skill, rows)
        assert detail.model_dump_json() == mappers.skill_detail(skill, rows).model_dump_json()

    async def test_the_version_listing_equals_the_one_built_from_fully_loaded_rows(
        self, world, session_factory, actors
    ):
        rows = await reference_rows(session_factory)

        listed = await world.versions.list(actors.owner, NAME)

        assert listed == [mappers.version_summary(row) for row in rows]

    async def test_the_public_view_hides_the_draft_exactly_as_before(
        self, world, session_factory, actors
    ):
        rows = [row for row in await reference_rows(session_factory) if row.version != "3.0.0"]

        listed = await world.versions.list(None, NAME)
        detail = await world.skills.get(None, NAME)

        assert listed == [mappers.version_summary(row) for row in rows]
        assert detail.versions == listed
        assert [item.version for item in listed] == ["2.0.0-beta.1", "1.1.0", "1.0.0"]

    async def test_latest_still_returns_the_complete_detail(self, world, session_factory, actors):
        rows = {row.version: row for row in await reference_rows(session_factory)}

        latest = await world.versions.get(None, NAME, "latest")
        exact = await world.versions.get(None, NAME, "1.1.0")

        assert latest == exact
        assert latest.metadata["notes"] == NOTES
        assert latest.metadata["build"] == "1.1.0"
        assert latest.version == "1.1.0"
        assert latest.sha256 == rows["1.1.0"].blob_sha256

    async def test_latest_still_serves_manifests_and_entrypoints(self, world, actors):
        listing = await world.packages.manifest(None, NAME)
        entrypoint = await world.packages.entrypoint(None, NAME)
        content = await world.packages.read_file(None, NAME, "latest", "docs/part-007.md")

        assert listing.version == entrypoint.version == "1.1.0"
        assert len(listing.files) == len(FILES) + 1
        assert "Instructions of 1.1.0." in entrypoint.content
        assert content.content == "part 7\n" * 3


class TestLatestLoadsOnlyTheVersionItReturns:
    async def test_the_latest_version_is_complete_and_its_siblings_stay_light(
        self, world, db_session, engine
    ):
        skill = await skill_repo.get_skill_by_name(db_session, NAME)

        with recorded(engine) as statements:
            latest = await catalog.get_latest_version(db_session, skill)

        assert latest.version == "1.1.0"
        assert not HEAVY_ATTRIBUTES & inspect(latest).unloaded
        heavy = [s for s in statements if reads_heavy_columns(s)]
        assert len(heavy) == 1
        assert reads_a_single_row(heavy[0])
        siblings = await skill_repo.list_versions(db_session, skill.id)
        assert {item.version for item in siblings} == set(VERSIONS)

    async def test_an_entry_without_a_published_version_has_no_latest(self, world, db_session):
        skill = await skill_repo.get_skill_by_name(db_session, NAME)
        await db_session.execute(update(SkillVersion).values(status=VersionStatus.DRAFT))

        with pytest.raises(NotFoundError, match="no published version"):
            await catalog.get_latest_version(db_session, skill)


class TestRefreshLoadsTheSourceSearchTextOnly:
    async def test_restore_reads_the_search_text_of_a_single_version(
        self, world, engine, session_factory
    ):
        await world.skills.hide(world.owner, NAME)

        with recorded(engine) as statements:
            detail = await world.skills.restore(world.owner, NAME)

        heavy = [s for s in statements if reads_heavy_columns(s)]
        assert len(heavy) == 1
        assert selected_columns(heavy[0]) == "SELECT skill_versions.search_text"
        assert reads_a_single_row(heavy[0])
        assert detail.status == "published"
        assert detail.latest_version == "1.1.0"
        skill = await reference_skill(session_factory)
        assert skill.search_text == body_of("1.1.0").strip()

    @pytest.mark.parametrize("operation", ["publish", "deprecate"])
    async def test_the_heavy_reads_of_a_lifecycle_change_do_not_grow_with_the_versions(
        self, world, engine, actors, operation
    ):
        for index in range(8):
            await upload_package(
                world.packages,
                actors.owner,
                "bulk-skill",
                f"1.{index}.0",
                files=FILES,
                body=body_of(f"1.{index}.0"),
                publish=True,
            )
        await upload_package(
            world.packages,
            actors.owner,
            "bulk-skill",
            "9.0.0",
            files=FILES,
            body=body_of("9.0.0"),
            publish=False,
        )

        async def heavy_reads(name: str, version: str) -> list[str]:
            with recorded(engine) as statements:
                if operation == "publish":
                    await world.versions.publish(world.owner, name, version)
                else:
                    await world.versions.deprecate(world.owner, name, version)
            return [s for s in statements if reads_heavy_columns(s)]

        few = await heavy_reads(NAME, "3.0.0" if operation == "publish" else "1.1.0")
        many = await heavy_reads("bulk-skill", "9.0.0" if operation == "publish" else "1.7.0")

        assert few
        assert len(few) == len(many)
        assert all(reads_a_single_row(statement) for statement in [*few, *many])

    async def test_uploading_a_version_never_scans_the_heavy_columns_of_every_version(
        self, world, engine, actors
    ):
        with recorded(engine) as statements:
            await upload_package(
                world.packages,
                actors.owner,
                NAME,
                "3.1.0",
                files=FILES,
                body=body_of("3.1.0"),
                publish=True,
            )

        heavy = [s for s in statements if reads_heavy_columns(s)]
        assert heavy
        assert all(reads_a_single_row(statement) for statement in heavy)

    async def test_the_denormalized_fields_follow_the_source_version_through_its_life(
        self, world, session_factory
    ):
        async def assert_follows_the_source():
            rows = await reference_rows(session_factory)
            skill = await reference_skill(session_factory)
            source = expected_source(rows)
            assert skill.search_text == source.search_text
            assert skill.display_name == source.display_name
            assert skill.description == source.description
            assert skill.category == source.category
            assert skill.tags == mappers.version_tags(source)
            published = [row for row in rows if row.status == VersionStatus.PUBLISHED]
            assert skill.latest_version == semver.pick_latest(row.version for row in published)

        await assert_follows_the_source()
        await world.versions.publish(world.owner, NAME, "3.0.0")
        await assert_follows_the_source()
        assert (await reference_skill(session_factory)).search_text == body_of("3.0.0").strip()
        await world.versions.deprecate(world.owner, NAME, "3.0.0")
        await assert_follows_the_source()
        assert (await reference_skill(session_factory)).search_text == body_of("1.1.0").strip()
        await world.versions.deprecate(world.owner, NAME, "1.1.0")
        await assert_follows_the_source()
        await world.versions.publish(world.owner, NAME, "1.1.0")
        await assert_follows_the_source()

    async def test_an_entry_with_only_drafts_describes_itself_with_its_highest_version(
        self, db_session, blob_store, actors, session_factory
    ):
        packages = PackageService(db_session, blob_store, public_read=True, auto_publish=False)
        for number in ("0.1.0", "0.3.0", "0.2.0"):
            await upload_package(
                packages,
                actors.owner,
                NAME,
                number,
                files=FILES,
                body=body_of(number),
                publish=False,
            )

        rows = await reference_rows(session_factory)
        skill = await reference_skill(session_factory)

        assert skill.status == SkillStatus.DRAFT
        assert skill.latest_version is None
        assert skill.search_text == body_of("0.3.0").strip()
        assert skill.search_text == expected_source(rows).search_text
