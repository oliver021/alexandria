from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import update

from app.config.settings import settings
from app.models import Skill, SkillKind, SkillStatus, VersionStatus
from app.models.schemas import PaginatedResult, SkillDetail, SkillSummary, TaxonomyItem
from app.repositories import skill_repo
from app.services.errors import (
    DomainError,
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
    ValidationError,
)
from app.services.package_service import PackageService
from app.services.skill_service import SkillService
from tests.helpers.factories import (
    ACTOR_NAMES,
    ENTRY_OWNERS,
    VISIBLE_TO,
    add_skill,
    add_version,
    record_writes,
    seed_catalog,
    upload_package,
    write_outcome,
)

SEARCH_VISIBLE = {
    "anonymous": ["foreign-skill", "pub-skill"],
    "stranger": ["foreign-skill", "pub-skill"],
    "stranger_read": ["foreign-skill", "pub-skill"],
    "owner": ["draft-skill", "foreign-skill", "hidden-ground", "pub-skill"],
    "owner_read": ["draft-skill", "foreign-skill", "hidden-ground", "pub-skill"],
    "admin": ["draft-skill", "foreign-skill", "hidden-ground", "pub-skill"],
    "admin_read": ["draft-skill", "foreign-skill", "hidden-ground", "pub-skill"],
}


SUMMARY_KEYS = frozenset(
    {
        "id",
        "name",
        "display_name",
        "description",
        "kind",
        "category",
        "tags",
        "status",
        "latest_version",
        "owner_id",
        "created_at",
        "updated_at",
    }
)

WRITE_CASES = [
    (actor, entry, write_outcome(actor, entry)) for entry in ENTRY_OWNERS for actor in ACTOR_NAMES
]
WRITE_IDS = [f"{actor}-{entry}" for actor, entry, _ in WRITE_CASES]


@pytest_asyncio.fixture
async def catalog(db_session, actors):
    return await seed_catalog(db_session, actors)


@pytest.fixture
def service(db_session):
    return SkillService(db_session, public_read=True)


@pytest.fixture
def private_service(db_session):
    return SkillService(db_session, public_read=False)


@pytest.fixture
def packages(db_session, blob_store):
    return PackageService(db_session, blob_store, public_read=True, auto_publish=True)


@pytest.fixture(params=[True, False], ids=["public-read", "private-read"])
def any_service(request, db_session):
    return SkillService(db_session, public_read=request.param)


async def stored(session_factory, name: str):
    async with session_factory() as session:
        skill = await skill_repo.get_skill_by_name(session, name)
        return None if skill is None else (skill.status, skill.latest_version, skill.tags)


class TestGet:
    @pytest.mark.parametrize(
        ("actor", "entry"),
        [(actor, entry) for entry in VISIBLE_TO for actor in ACTOR_NAMES],
    )
    async def test_visibility_matrix(self, service, actors, catalog, actor, entry):
        principal = actors.get(actor)
        if actor in VISIBLE_TO[entry]:
            detail = await service.get(principal, entry)
            assert isinstance(detail, SkillDetail)
            assert detail.name == entry
        else:
            with pytest.raises(NotFoundError):
                await service.get(principal, entry)

    async def test_invisible_and_missing_entries_are_indistinguishable(
        self, service, actors, catalog
    ):
        with pytest.raises(NotFoundError) as hidden:
            await service.get(actors.stranger, "hidden-ground")
        with pytest.raises(NotFoundError) as draft:
            await service.get(actors.anonymous, "draft-skill")
        with pytest.raises(NotFoundError) as missing:
            await service.get(actors.stranger, "never-existed")
        assert hidden.value.code == draft.value.code == missing.value.code == "NOT_FOUND"
        assert not isinstance(hidden.value, ForbiddenError)
        shape = hidden.value.message.replace("hidden-ground", "<ref>")
        assert shape == missing.value.message.replace("never-existed", "<ref>")
        assert shape == draft.value.message.replace("draft-skill", "<ref>")

    async def test_returns_the_entry_with_every_summary_field(self, service, actors, catalog):
        detail = await service.get(actors.anonymous, "pub-skill")

        assert detail.id == catalog.published_id
        assert detail.name == "pub-skill"
        assert detail.display_name == "Pub Skill"
        assert detail.description == "Published skill"
        assert detail.kind == "skill"
        assert detail.category == "ops"
        assert detail.tags == ["alpha", "beta"]
        assert detail.status == "published"
        assert detail.latest_version == "1.1.0"
        assert detail.owner_id == actors.owner.user_id
        assert detail.created_at.tzinfo is UTC
        assert detail.updated_at.tzinfo is UTC

    async def test_grounding_entries_report_their_kind(self, service, actors, catalog):
        detail = await service.get(actors.owner, "hidden-ground")
        assert detail.kind == "grounding"
        assert detail.status == "unpublished"

    async def test_lookup_by_id_matches_lookup_by_name(self, service, actors, catalog):
        by_id = await service.get(actors.anonymous, catalog.published_id)
        by_name = await service.get(actors.anonymous, "pub-skill")
        assert by_id == by_name

    async def test_uppercase_and_alternative_uuid_spellings_resolve_by_id(
        self, service, actors, catalog
    ):
        upper = await service.get(actors.anonymous, catalog.published_id.upper())
        compact = await service.get(actors.anonymous, catalog.published_id.replace("-", ""))
        assert upper.name == compact.name == "pub-skill"

    async def test_an_unknown_uuid_is_not_found(self, service, actors, catalog):
        with pytest.raises(NotFoundError):
            await service.get(actors.anonymous, "00000000-0000-4000-8000-000000000000")

    @pytest.mark.parametrize(
        "ref", ["", " ", "pub-skill ", "PUB-SKILL", "pub\x00skill", "x" * 5000]
    )
    async def test_malformed_references_are_not_found(self, service, actors, catalog, ref):
        with pytest.raises(NotFoundError):
            await service.get(actors.admin, ref)

    @pytest.mark.parametrize("ref", ["pub\x00skill", "\x00", "", 7, None])
    async def test_unusable_references_never_reach_the_database(
        self, service, actors, catalog, monkeypatch, ref
    ):
        async def forbidden(*args, **kwargs):
            raise AssertionError("the database must not be queried")

        monkeypatch.setattr(skill_repo, "get_skill_by_name", forbidden)
        monkeypatch.setattr(skill_repo, "get_skill", forbidden)

        with pytest.raises(NotFoundError):
            await service.get(actors.admin, ref)

    async def test_a_non_string_reference_is_not_found(self, service, actors, catalog):
        with pytest.raises(NotFoundError):
            await service.get(actors.admin, None)

    async def test_versions_are_listed_by_semver_descending_and_filtered_by_visibility(
        self, service, actors, catalog
    ):
        for actor in ("anonymous", "stranger", "stranger_read"):
            detail = await service.get(actors.get(actor), "pub-skill")
            assert [item.version for item in detail.versions] == [
                "2.0.0-beta.1",
                "1.1.0",
                "1.0.0",
                "0.9.0",
            ]
        for actor in ("owner", "owner_read", "admin", "admin_read"):
            detail = await service.get(actors.get(actor), "pub-skill")
            assert [item.version for item in detail.versions] == [
                "2.0.0",
                "2.0.0-beta.1",
                "1.1.0",
                "1.0.0",
                "0.9.0",
            ]

    async def test_deprecated_versions_are_visible_to_everyone_and_marked(
        self, service, actors, catalog
    ):
        detail = await service.get(actors.anonymous, "pub-skill")
        statuses = {item.version: item.status for item in detail.versions}
        assert statuses["0.9.0"] == "deprecated"
        assert statuses["1.1.0"] == "published"
        assert "2.0.0" not in statuses

    async def test_a_draft_only_entry_exposes_its_draft_version_to_the_owner(
        self, service, actors, catalog
    ):
        detail = await service.get(actors.owner, "draft-skill")
        assert detail.status == "draft"
        assert detail.latest_version is None
        assert [(item.version, item.status) for item in detail.versions] == [("0.1.0", "draft")]

    async def test_version_summaries_carry_the_stored_metadata(self, service, actors, catalog):
        detail = await service.get(actors.anonymous, "pub-skill")
        version = next(item for item in detail.versions if item.version == "1.1.0")
        assert version.skill_id == catalog.published_id
        assert version.sha256 == "a" * 64
        assert (version.size_bytes, version.uncompressed_bytes, version.file_count) == (100, 200, 1)
        assert version.entrypoint == "SKILL.md"
        assert version.tags == ["alpha", "beta"]
        assert version.uploaded_by == actors.owner.user_id
        assert version.published_by == actors.owner.user_id
        assert version.published_at is not None
        assert version.deprecated_at is None

    async def test_the_dump_uses_only_json_friendly_values(self, service, actors, catalog):
        detail = await service.get(actors.anonymous, "pub-skill")
        dumped = detail.model_dump(mode="json")
        assert dumped["kind"] == "skill"
        assert dumped["status"] == "published"
        assert dumped["created_at"].endswith(("Z", "+00:00"))
        assert dumped["versions"][0]["status"] in {"draft", "published", "deprecated"}


class TestContractShapes:
    async def test_summaries_details_pages_and_taxonomy_items_have_the_contract_keys(
        self, service, actors, catalog
    ):
        page = (await service.search(None)).model_dump(mode="json")
        detail = (await service.get(None, "pub-skill")).model_dump(mode="json")
        tags = (await service.tags(None))[0].model_dump(mode="json")

        assert set(page) == {"items", "total", "limit", "offset"}
        assert set(page["items"][0]) == SUMMARY_KEYS
        assert set(detail) == SUMMARY_KEYS | {"versions"}
        assert set(tags) == {"name", "count"}
        assert isinstance(detail["tags"], list) and isinstance(detail["versions"], list)

    async def test_timestamps_are_iso_8601_in_utc(self, service, actors, catalog):
        dumped = (await service.get(None, "pub-skill")).model_dump(mode="json")
        for value in (dumped["created_at"], dumped["updated_at"]):
            assert datetime.fromisoformat(value).utcoffset() == timedelta(0)
        for version in dumped["versions"]:
            assert datetime.fromisoformat(version["created_at"]).utcoffset() == timedelta(0)


class TestPrivateRegistry:
    @pytest.mark.parametrize(
        "call",
        [
            lambda svc, p: svc.get(p, "pub-skill"),
            lambda svc, p: svc.search(p),
            lambda svc, p: svc.tags(p),
            lambda svc, p: svc.categories(p),
        ],
        ids=["get", "search", "tags", "categories"],
    )
    async def test_anonymous_reads_are_unauthorized(self, private_service, actors, catalog, call):
        with pytest.raises(UnauthorizedError):
            await call(private_service, None)

    @pytest.mark.parametrize("actor", [name for name in ACTOR_NAMES if name != "anonymous"])
    async def test_any_principal_still_reads(self, private_service, actors, catalog, actor):
        principal = actors.get(actor)
        assert (await private_service.get(principal, "pub-skill")).name == "pub-skill"
        assert (await private_service.search(principal)).total >= 2
        assert await private_service.tags(principal)
        assert await private_service.categories(principal)

    async def test_visibility_rules_still_apply_to_authenticated_readers(
        self, private_service, actors, catalog
    ):
        with pytest.raises(NotFoundError):
            await private_service.get(actors.stranger, "draft-skill")

    async def test_the_default_comes_from_the_settings(
        self, db_session, actors, catalog, monkeypatch
    ):
        monkeypatch.setattr(settings.registry, "public_read", False)
        with pytest.raises(UnauthorizedError):
            await SkillService(db_session).get(None, "pub-skill")
        monkeypatch.setattr(settings.registry, "public_read", True)
        assert (await SkillService(db_session).get(None, "pub-skill")).name == "pub-skill"

    async def test_an_explicit_argument_overrides_the_settings(
        self, db_session, actors, catalog, monkeypatch
    ):
        monkeypatch.setattr(settings.registry, "public_read", True)
        with pytest.raises(UnauthorizedError):
            await SkillService(db_session, public_read=False).get(None, "pub-skill")


class TestSearch:
    @pytest.mark.parametrize("actor", ACTOR_NAMES)
    async def test_each_actor_lists_exactly_the_entries_it_may_see(
        self, service, actors, catalog, actor
    ):
        result = await service.search(actors.get(actor))
        assert [item.name for item in result.items] == SEARCH_VISIBLE[actor]
        assert result.total == len(SEARCH_VISIBLE[actor])

    async def test_the_result_is_a_paginated_result_of_summaries(self, service, actors, catalog):
        result = await service.search(actors.anonymous)
        assert isinstance(result, PaginatedResult)
        assert all(isinstance(item, SkillSummary) for item in result.items)
        assert (result.limit, result.offset) == (20, 0)

    async def test_pagination_slices_and_reports_the_real_total(self, service, actors, catalog):
        first = await service.search(actors.admin, limit=3, offset=0)
        second = await service.search(actors.admin, limit=3, offset=3)
        beyond = await service.search(actors.admin, limit=3, offset=50)
        assert [item.name for item in first.items] == [
            "draft-skill",
            "foreign-skill",
            "hidden-ground",
        ]
        assert [item.name for item in second.items] == ["pub-skill"]
        assert beyond.items == []
        assert first.total == second.total == beyond.total == 4
        assert (second.limit, second.offset) == (3, 3)

    async def test_mine_lists_only_entries_owned_by_the_caller_including_private_ones(
        self, service, actors, catalog
    ):
        owner = await service.search(actors.owner, mine=True)
        stranger = await service.search(actors.stranger, mine=True)
        admin = await service.search(actors.admin, mine=True)
        assert [item.name for item in owner.items] == ["draft-skill", "hidden-ground", "pub-skill"]
        assert [item.name for item in stranger.items] == ["foreign-skill"]
        assert admin.items == []

    async def test_mine_requires_a_principal(self, service, actors, catalog):
        with pytest.raises(UnauthorizedError):
            await service.search(None, mine=True)

    @pytest.mark.parametrize(
        ("actor", "status", "expected"),
        [
            ("owner", "draft", ["draft-skill"]),
            ("owner", "unpublished", ["hidden-ground"]),
            ("owner", "published", ["foreign-skill", "pub-skill"]),
            ("owner_read", "draft", ["draft-skill"]),
            ("admin", "draft", ["draft-skill"]),
            ("admin", "unpublished", ["hidden-ground"]),
            ("admin", "published", ["foreign-skill", "pub-skill"]),
            ("stranger", "draft", []),
            ("stranger", "unpublished", []),
            ("stranger", "published", ["foreign-skill", "pub-skill"]),
            ("anonymous", "draft", []),
            ("anonymous", "unpublished", []),
            ("anonymous", "published", ["foreign-skill", "pub-skill"]),
        ],
    )
    async def test_the_status_filter_never_widens_visibility(
        self, service, actors, catalog, actor, status, expected
    ):
        result = await service.search(actors.get(actor), status=status)
        assert [item.name for item in result.items] == expected
        assert result.total == len(expected)

    async def test_status_accepts_enum_members_and_ignores_blank_values(
        self, service, actors, catalog
    ):
        by_enum = await service.search(actors.owner, status=SkillStatus.DRAFT)
        blank = await service.search(actors.owner, status="  ")
        assert [item.name for item in by_enum.items] == ["draft-skill"]
        assert blank.total == 4

    @pytest.mark.parametrize(
        ("actor", "kind", "expected"),
        [
            ("anonymous", "grounding", []),
            ("owner", "grounding", ["hidden-ground"]),
            ("admin", "skill", ["draft-skill", "foreign-skill", "pub-skill"]),
            ("owner", SkillKind.GROUNDING, ["hidden-ground"]),
            ("owner", " Grounding ", ["hidden-ground"]),
        ],
    )
    async def test_kind_filter(self, service, actors, catalog, actor, kind, expected):
        result = await service.search(actors.get(actor), kind=kind)
        assert [item.name for item in result.items] == expected

    async def test_tag_filters_combine_with_and_and_ignore_case(self, service, actors, catalog):
        both = await service.search(None, tags=["ALPHA", "beta"])
        single = await service.search(None, tags="alpha")
        missing = await service.search(None, tags=["alpha", "nope"])
        assert [item.name for item in both.items] == ["pub-skill"]
        assert [item.name for item in single.items] == ["pub-skill"]
        assert missing.items == []

    async def test_omitted_filters_are_accepted(self, service, actors, catalog):
        result = await service.search(None, tags=None, category=None, q=None, kind=None)
        assert result.total == 2

    async def test_category_filter(self, service, actors, catalog):
        result = await service.search(None, category=" OPS ")
        assert [item.name for item in result.items] == ["pub-skill"]

    async def test_text_query_matches_names_and_descriptions(self, service, actors, catalog):
        by_name = await service.search(None, q="pub")
        by_description = await service.search(None, q="published skill")
        none = await service.search(None, q="zzz-nothing")
        assert [item.name for item in by_name.items] == ["pub-skill"]
        assert [item.name for item in by_description.items] == ["pub-skill"]
        assert none.total == 0

    async def test_text_query_cannot_reveal_private_entries(self, service, actors, catalog):
        assert (await service.search(None, q="draft")).total == 0
        assert (await service.search(actors.stranger, q="hidden-ground")).total == 0
        assert (await service.search(actors.owner, q="hidden-ground")).total == 1

    async def test_relevance_is_the_default_order_with_a_query(self, service, db_session, actors):
        owner = actors.owner
        await add_skill(db_session, owner, "alpha-tool", description="mentions beta here")
        await add_skill(db_session, owner, "beta", description="plain")
        await add_skill(db_session, owner, "gamma-beta", description="plain")
        result = await service.search(None, q="beta")
        assert [item.name for item in result.items] == ["beta", "gamma-beta", "alpha-tool"]

    @pytest.mark.parametrize("sort", ["name", "updated", "created", "relevance", " NAME "])
    async def test_valid_sort_options(self, service, actors, catalog, sort):
        assert (await service.search(None, sort=sort)).total == 2

    async def test_the_sort_option_reaches_the_query(self, service, db_session, actors, catalog):
        await db_session.execute(
            update(Skill)
            .where(Skill.name == "pub-skill")
            .values(created_at=datetime(2026, 1, 1), updated_at=datetime(2030, 1, 1))
        )
        await db_session.execute(
            update(Skill)
            .where(Skill.name == "foreign-skill")
            .values(created_at=datetime(2020, 1, 1), updated_at=datetime(2021, 1, 1))
        )
        await db_session.commit()

        async def order(**arguments):
            return [item.name for item in (await service.search(None, **arguments)).items]

        assert await order() == ["foreign-skill", "pub-skill"]
        assert await order(sort="name") == ["foreign-skill", "pub-skill"]
        assert await order(sort="updated") == ["pub-skill", "foreign-skill"]
        assert await order(sort="created") == ["pub-skill", "foreign-skill"]

    async def test_an_explicit_sort_overrides_relevance(self, service, db_session, actors):
        for name, description in (
            ("alpha-tool", "mentions beta here"),
            ("beta", "plain"),
            ("gamma-beta", "plain"),
        ):
            await add_skill(db_session, actors.owner, name, description=description)

        async def order(**arguments):
            return [item.name for item in (await service.search(None, q="beta", **arguments)).items]

        assert await order() == ["beta", "gamma-beta", "alpha-tool"]
        assert await order(sort="relevance") == ["beta", "gamma-beta", "alpha-tool"]
        assert await order(sort="name") == ["alpha-tool", "beta", "gamma-beta"]

    async def test_a_blank_sort_means_the_default(self, service, actors, catalog):
        assert (await service.search(None, sort="")).total == 2

    @pytest.mark.parametrize("sort", ["popularity", "name;drop", "nombre", 3, ["name"]])
    async def test_unknown_sort_is_a_validation_error(self, service, actors, catalog, sort):
        with pytest.raises(ValidationError):
            await service.search(None, sort=sort)

    @pytest.mark.parametrize("limit", [0, -1, 101, 1000, "5", 1.5, None, True])
    async def test_limit_must_be_between_one_and_one_hundred(self, service, actors, catalog, limit):
        with pytest.raises(ValidationError):
            await service.search(None, limit=limit)

    @pytest.mark.parametrize("limit", [1, 50, 100])
    async def test_valid_limits(self, service, actors, catalog, limit):
        assert (await service.search(None, limit=limit)).limit == limit

    @pytest.mark.parametrize("offset", [-1, 2**31, 2**70, "0", 0.5, None, False])
    async def test_offset_must_be_a_bounded_non_negative_integer(
        self, service, actors, catalog, offset
    ):
        with pytest.raises(ValidationError):
            await service.search(None, offset=offset)

    async def test_the_largest_accepted_offset_runs_and_the_next_one_is_rejected(
        self, service, actors, catalog
    ):
        result = await service.search(None, offset=2**31 - 1)
        assert result.items == [] and result.total == 2 and result.offset == 2**31 - 1
        with pytest.raises(ValidationError):
            await service.search(None, offset=2**31)

    @pytest.mark.parametrize("kind", ["service", "skills", 7, ["skill"]])
    async def test_unknown_kind_is_a_validation_error(self, service, actors, catalog, kind):
        with pytest.raises(ValidationError):
            await service.search(None, kind=kind)

    @pytest.mark.parametrize("status", ["archived", "PUBLISHED!", 1, ["draft"]])
    async def test_unknown_status_is_a_validation_error(self, service, actors, catalog, status):
        with pytest.raises(ValidationError):
            await service.search(actors.owner, status=status)

    @pytest.mark.parametrize(
        "arguments",
        [
            {"q": 123},
            {"q": ["a"]},
            {"q": "a\x00b"},
            {"q": "x" * 513},
            {"category": 5},
            {"category": "a\x00"},
            {"category": "c" * 257},
            {"tags": 5},
            {"tags": [1]},
            {"tags": ["ok", None]},
            {"tags": ["a\x00"]},
            {"tags": ["t" * 257]},
            {"tags": "t" * 257},
        ],
    )
    async def test_malformed_filters_are_validation_errors(
        self, service, actors, catalog, arguments
    ):
        with pytest.raises(ValidationError):
            await service.search(None, **arguments)

    async def test_the_longest_accepted_query_runs(self, service, actors, catalog):
        result = await service.search(None, q="x" * 512)
        assert result.total == 0

    async def test_more_than_twenty_tag_filters_return_nothing(self, service, actors, catalog):
        result = await service.search(None, tags=[f"t{index}" for index in range(21)])
        assert result.items == [] and result.total == 0

    async def test_validation_errors_are_domain_errors_with_a_code(self, service):
        with pytest.raises(DomainError) as error:
            await service.search(None, limit=0)
        assert error.value.code == "VALIDATION_ERROR"

    async def test_search_does_not_write(self, service, db_session, actors, catalog):
        await service.search(actors.admin)
        assert not db_session.new
        assert not db_session.dirty
        assert not db_session.deleted


class TestUnicodeSearch:
    @pytest_asyncio.fixture
    async def library(self, packages, actors):
        owner = actors.owner
        await upload_package(
            packages, owner, "alpha-one", description="Árbol de decisión para ÉCOLE"
        )
        await upload_package(
            packages,
            owner,
            "bravo-two",
            display_name="Índice Útil Über",
            description="plain words",
        )
        await upload_package(
            packages,
            owner,
            "charlie-three",
            description="plain words",
            body="Ñandú ÁRBOL MÉTODO\n",
        )

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("árbol", ["alpha-one", "charlie-three"]),
            ("ÁRBOL", ["alpha-one", "charlie-three"]),
            ("Árbol", ["alpha-one", "charlie-three"]),
            ("árbol de", ["alpha-one"]),
            ("ÁRBOL DE DECISIÓN", ["alpha-one"]),
            ("école", ["alpha-one"]),
            ("ÉCOLE", ["alpha-one"]),
            ("decisión", ["alpha-one"]),
            ("ñandú", ["charlie-three"]),
            ("ÑANDÚ", ["charlie-three"]),
            ("método", ["charlie-three"]),
            ("MÉTODO", ["charlie-three"]),
            ("índice", ["bravo-two"]),
            ("ÍNDICE", ["bravo-two"]),
            ("útil", ["bravo-two"]),
            ("über", ["bravo-two"]),
            ("árbol método", ["charlie-three"]),
            ("árbol útil", []),
        ],
    )
    async def test_capitalised_non_ascii_text_is_found_whatever_the_case_of_the_query(
        self, service, actors, library, query, expected
    ):
        page = await service.search(actors.anonymous, q=query)

        assert sorted(item.name for item in page.items) == expected
        assert page.total == len(expected)

    async def test_the_library_really_stores_capitalised_text_in_each_field(
        self, service, actors, library, db_session
    ):
        by_description = await service.get(None, "alpha-one")
        by_display_name = await service.get(None, "bravo-two")
        assert by_description.description == "Árbol de decisión para ÉCOLE"
        assert by_display_name.display_name == "Índice Útil Über"
        stored = await skill_repo.get_skill_by_name(db_session, "charlie-three")
        assert "ÁRBOL" in stored.search_text

    async def test_relevance_weights_apply_to_non_ascii_matches(self, service, packages, actors):
        owner = actors.owner
        await upload_package(
            packages,
            owner,
            "rank-body",
            display_name="Zulu",
            description="plain words",
            body="el ÁRBOL\n",
        )
        await upload_package(
            packages,
            owner,
            "rank-description",
            display_name="Yankee",
            description="un ÁRBOL grande",
        )
        await upload_package(
            packages, owner, "rank-display", display_name="Árbol", description="plain words"
        )

        page = await service.search(None, q="árbol")

        assert [item.name for item in page.items] == [
            "rank-display",
            "rank-description",
            "rank-body",
        ]

    async def test_non_ascii_queries_cannot_reveal_private_entries(self, service, packages, actors):
        await upload_package(
            packages, actors.owner, "secret-tree", publish=False, description="Árbol secreto"
        )

        assert (await service.search(None, q="ÁRBOL")).total == 0
        assert (await service.search(actors.stranger, q="árbol")).total == 0
        owner_page = await service.search(actors.owner, q="árbol")
        assert [item.name for item in owner_page.items] == ["secret-tree"]


class TestHide:
    @pytest.mark.parametrize(("actor", "entry", "outcome"), WRITE_CASES, ids=WRITE_IDS)
    async def test_authorization_matrix(
        self, any_service, actors, catalog, session_factory, actor, entry, outcome
    ):
        service = any_service
        principal = actors.get(actor)
        before = await stored(session_factory, entry)
        if outcome is None:
            assert await service.hide(principal, entry) is None
            status, _, _ = await stored(session_factory, entry)
            assert status == SkillStatus.UNPUBLISHED
        else:
            with pytest.raises(outcome):
                await service.hide(principal, entry)
            assert await stored(session_factory, entry) == before

    @pytest.mark.parametrize("actor", ["anonymous", "stranger", "stranger_read", "admin_read"])
    async def test_missing_entries_never_succeed(self, service, actors, catalog, actor):
        expected = {
            "anonymous": UnauthorizedError,
            "stranger": NotFoundError,
            "stranger_read": ForbiddenError,
            "admin_read": ForbiddenError,
        }[actor]
        with pytest.raises(expected):
            await service.hide(actors.get(actor), "no-such-entry")

    async def test_a_read_scope_probe_cannot_tell_hidden_entries_from_missing_ones(
        self, service, actors, catalog
    ):
        errors = []
        for ref in ("draft-skill", "hidden-ground", "no-such-entry"):
            with pytest.raises(ForbiddenError) as raised:
                await service.hide(actors.stranger_read, ref)
            errors.append(raised.value.message)
        assert len(set(errors)) == 1

    async def test_hiding_removes_the_entry_from_anonymous_reads_but_keeps_it_for_the_owner(
        self, service, actors, catalog
    ):
        await service.hide(actors.owner, "pub-skill")

        with pytest.raises(NotFoundError):
            await service.get(None, "pub-skill")
        assert "pub-skill" not in [item.name for item in (await service.search(None)).items]
        detail = await service.get(actors.owner, "pub-skill")
        assert detail.status == "unpublished"
        assert detail.latest_version == "1.1.0"
        assert len(detail.versions) == 5

    async def test_hiding_is_idempotent(self, service, actors, catalog):
        await service.hide(actors.owner, "hidden-ground")
        await service.hide(actors.admin, "hidden-ground")
        assert (await service.get(actors.owner, "hidden-ground")).status == "unpublished"

    async def test_hiding_an_already_hidden_entry_writes_nothing(
        self, service, engine, actors, catalog
    ):
        with record_writes(engine) as writes:
            await service.hide(actors.owner, "hidden-ground")
        assert writes == []

        with record_writes(engine) as writes:
            await service.hide(actors.owner, "pub-skill")
        assert len(writes) == 1 and writes[0].startswith("UPDATE skills")

    async def test_an_admin_can_hide_an_entry_of_another_user(
        self, service, actors, catalog, session_factory
    ):
        await service.hide(actors.admin, "foreign-skill")
        status, _, _ = await stored(session_factory, "foreign-skill")
        assert status == SkillStatus.UNPUBLISHED

    async def test_hiding_persists_across_sessions(self, service, actors, catalog, session_factory):
        await service.hide(actors.owner, "pub-skill")
        async with session_factory() as other:
            fresh = await SkillService(other, public_read=True).get(actors.owner, "pub-skill")
        assert fresh.status == "unpublished"


class TestRestore:
    @pytest.mark.parametrize(("actor", "entry", "outcome"), WRITE_CASES, ids=WRITE_IDS)
    async def test_authorization_matrix(self, any_service, actors, catalog, actor, entry, outcome):
        service = any_service
        principal = actors.get(actor)
        if outcome is None:
            detail = await service.restore(principal, entry)
            assert isinstance(detail, SkillDetail)
            assert detail.name == entry
        else:
            with pytest.raises(outcome):
                await service.restore(principal, entry)

    async def test_restoring_an_entry_with_a_published_version_publishes_it_again(
        self, service, actors, catalog
    ):
        detail = await service.restore(actors.owner, "hidden-ground")

        assert detail.status == "published"
        assert detail.latest_version == "1.0.0"
        assert (await service.get(None, "hidden-ground")).status == "published"
        assert "hidden-ground" in [item.name for item in (await service.search(None)).items]

    async def test_restoring_an_entry_without_published_versions_leaves_it_as_draft(
        self, service, actors, catalog
    ):
        await service.hide(actors.owner, "draft-skill")
        assert (await service.get(actors.owner, "draft-skill")).status == "unpublished"

        detail = await service.restore(actors.owner, "draft-skill")

        assert detail.status == "draft"
        assert detail.latest_version is None
        with pytest.raises(NotFoundError):
            await service.get(None, "draft-skill")

    async def test_restore_recomputes_the_denormalized_fields_from_the_versions(
        self, service, db_session, actors, catalog
    ):
        await service.hide(actors.owner, "pub-skill")
        skill = await skill_repo.get_skill_by_name(db_session, "pub-skill")
        await skill_repo.update_skill_denormalized(
            db_session,
            skill,
            display_name="Stale",
            description="Stale",
            category=None,
            search_text=None,
            latest_version="0.0.1",
            tags=["stale"],
        )
        await db_session.commit()

        detail = await service.restore(actors.owner, "pub-skill")

        assert detail.latest_version == "1.1.0"
        assert detail.display_name == "Seeded"
        assert detail.description == "Seeded version"
        assert detail.tags == ["alpha", "beta"]
        assert detail.status == "published"

    async def test_restoring_an_entry_without_any_version_only_recomputes_its_status(
        self, service, db_session, actors
    ):
        await add_skill(
            db_session,
            actors.owner,
            "empty-shell",
            status=SkillStatus.UNPUBLISHED,
            latest_version=None,
        )

        detail = await service.restore(actors.owner, "empty-shell")

        assert (detail.status, detail.latest_version, detail.versions) == ("draft", None, [])

    async def test_restoring_a_visible_entry_is_a_harmless_recomputation(
        self, service, actors, catalog
    ):
        detail = await service.restore(actors.owner, "pub-skill")
        assert detail.status == "published"
        assert detail.latest_version == "1.1.0"

    async def test_an_admin_can_restore_an_entry_of_another_user(self, service, actors, catalog):
        await service.hide(actors.stranger, "foreign-skill")
        detail = await service.restore(actors.admin, "foreign-skill")
        assert detail.status == "published"
        assert detail.owner_id == actors.stranger.user_id

    async def test_the_detail_lists_every_version_for_the_owner(self, service, actors, catalog):
        detail = await service.restore(actors.owner, "pub-skill")
        assert detail.versions[0].version == "2.0.0"
        assert len(detail.versions) == 5

    async def test_restore_persists_across_sessions(
        self, service, actors, catalog, session_factory
    ):
        await service.restore(actors.owner, "hidden-ground")
        status, latest, _ = await stored(session_factory, "hidden-ground")
        assert (status, latest) == (SkillStatus.PUBLISHED, "1.0.0")


class TestTaxonomy:
    @pytest_asyncio.fixture
    async def tagged(self, db_session, actors, catalog):
        owner = actors.owner
        extra = await add_skill(
            db_session,
            owner,
            "other-pub",
            kind=SkillKind.GROUNDING,
            category="ops",
            tags=("alpha",),
        )
        await add_version(db_session, extra, "1.0.0", owner, status=VersionStatus.PUBLISHED)
        secret = await add_skill(
            db_session,
            owner,
            "secret-draft",
            status=SkillStatus.DRAFT,
            latest_version=None,
            category="private",
            tags=("secret", "alpha"),
        )
        await add_version(db_session, secret, "0.1.0", owner, status=VersionStatus.DRAFT)

    @pytest.mark.parametrize(
        ("actor", "expected"),
        [
            ("anonymous", [("alpha", 2), ("beta", 1)]),
            ("stranger", [("alpha", 2), ("beta", 1)]),
            ("stranger_read", [("alpha", 2), ("beta", 1)]),
            ("owner", [("alpha", 3), ("beta", 1), ("secret", 1)]),
            ("owner_read", [("alpha", 3), ("beta", 1), ("secret", 1)]),
            ("admin", [("alpha", 3), ("beta", 1), ("secret", 1)]),
        ],
    )
    async def test_tags_count_only_visible_entries(self, service, actors, tagged, actor, expected):
        items = await service.tags(actors.get(actor))
        assert all(isinstance(item, TaxonomyItem) for item in items)
        assert [(item.name, item.count) for item in items] == expected

    @pytest.mark.parametrize(
        ("actor", "expected"),
        [
            ("anonymous", [("ops", 2)]),
            ("stranger", [("ops", 2)]),
            ("owner", [("ops", 2), ("private", 1)]),
            ("admin_read", [("ops", 2), ("private", 1)]),
        ],
    )
    async def test_categories_count_only_visible_entries(
        self, service, actors, tagged, actor, expected
    ):
        items = await service.categories(actors.get(actor))
        assert [(item.name, item.count) for item in items] == expected

    async def test_kind_filter_applies_to_both_listings(self, service, actors, tagged):
        tags = await service.tags(None, kind="grounding")
        categories = await service.categories(None, kind=SkillKind.GROUNDING)
        assert [(item.name, item.count) for item in tags] == [("alpha", 1)]
        assert [(item.name, item.count) for item in categories] == [("ops", 1)]

    async def test_limit_truncates_the_listing(self, service, actors, tagged):
        tags = await service.tags(actors.owner, limit=1)
        assert [(item.name, item.count) for item in tags] == [("alpha", 3)]

    @pytest.mark.parametrize("limit", [0, -5, 201, "10", None, True])
    async def test_invalid_limits_are_validation_errors(self, service, tagged, limit):
        with pytest.raises(ValidationError):
            await service.tags(None, limit=limit)
        with pytest.raises(ValidationError):
            await service.categories(None, limit=limit)

    async def test_the_largest_accepted_limit_runs(self, service, tagged):
        assert len(await service.tags(None, limit=200)) == 2

    async def test_unknown_kind_is_a_validation_error(self, service, tagged):
        with pytest.raises(ValidationError):
            await service.tags(None, kind="nope")
        with pytest.raises(ValidationError):
            await service.categories(None, kind="nope")

    async def test_hidden_entries_stop_counting_for_anonymous_readers(
        self, service, actors, tagged
    ):
        await service.hide(actors.owner, "pub-skill")
        tags = await service.tags(None)
        assert [(item.name, item.count) for item in tags] == [("alpha", 1)]
