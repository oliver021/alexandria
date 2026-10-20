from __future__ import annotations

import json
import random
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import event, inspect, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.models import (
    Skill,
    SkillKind,
    SkillStatus,
    SkillTag,
    SkillVersion,
    User,
    UserRole,
    VersionStatus,
)
from app.repositories import skill_repo
from app.repositories.skill_repo import SkillQuery, escape_like, normalize_tags, split_terms
from tests.test_repositories import create_test_engine, make_user


@pytest_asyncio.fixture
async def engine():
    test_engine = await create_test_engine()
    yield test_engine
    await test_engine.dispose()


@pytest_asyncio.fixture
async def db(engine: AsyncEngine):
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session


@pytest_asyncio.fixture
async def alice(db: AsyncSession) -> User:
    return await make_user(db, "alice")


@pytest_asyncio.fixture
async def bob(db: AsyncSession) -> User:
    return await make_user(db, "bob")


@pytest_asyncio.fixture
async def root(db: AsyncSession) -> User:
    return await make_user(db, "root", UserRole.ADMIN)


async def add_skill(
    db: AsyncSession,
    owner: User,
    name: str,
    *,
    display_name: str | None = None,
    description: str = "",
    kind: SkillKind = SkillKind.SKILL,
    category: str | None = None,
    tags: tuple[str, ...] | list[str] = (),
    search_text: str | None = None,
    status: SkillStatus = SkillStatus.PUBLISHED,
    latest_version: str | None = "1.0.0",
) -> Skill:
    return await skill_repo.create_skill(
        db,
        name=name,
        display_name=display_name or name,
        description=description,
        kind=kind,
        owner_id=owner.id,
        category=category,
        search_text=search_text,
        tags=tags,
        status=status,
        latest_version=latest_version,
    )


async def add_version(
    db: AsyncSession, skill: Skill, uploader: User, version: str = "1.0.0", **overrides: Any
):
    values: dict[str, Any] = {
        "skill_id": skill.id,
        "version": version,
        "blob_sha256": "a" * 64,
        "size_bytes": 100,
        "uncompressed_bytes": 200,
        "file_count": 2,
        "entrypoint": "SKILL.md",
        "display_name": skill.display_name,
        "description": skill.description,
        "uploaded_by": uploader.id,
    }
    values.update(overrides)
    return await skill_repo.create_version(db, **values)


@contextmanager
def record_statements(engine: AsyncEngine) -> Iterator[list[tuple[str, Any]]]:
    statements: list[tuple[str, Any]] = []

    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append((statement, parameters))

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)


UNICODE_VOCABULARY = (
    "Árbol",
    "ÁRBOL",
    "árbol",
    "École",
    "ÉCOLE",
    "école",
    "Ñandú",
    "ÑANDÚ",
    "ñandú",
    "Índice",
    "Útil",
    "Über",
    "MÉTODO",
    "método",
    "Привет",
    "ПРИВЕТ",
    "ΑΘΗΝΑ",
    "αθηνα",
    "Łódź",
    "ŁÓDŹ",
    "Straße",
    "STRASSE",
    "İstanbul",
    "Æther",
    "ÆTHER",
    "plain",
    "PLAIN",
    "data",
    "DATA",
)
UNICODE_TAGS = ("alpha", "beta", "data", "plain")
CASE_TRANSFORMS = (str.upper, str.lower, str.title, str.swapcase, str)


def random_words(rng: random.Random, low: int, high: int) -> str:
    return " ".join(rng.choices(UNICODE_VOCABULARY, k=rng.randint(low, high)))


def unicode_corpus(rng: random.Random, size: int) -> list[dict[str, Any]]:
    return [
        {
            "name": f"entry-{index:02d}",
            "display_name": random_words(rng, 1, 1),
            "description": random_words(rng, 1, 2),
            "search_text": random_words(rng, 1, 3) if rng.random() < 0.5 else None,
            "tags": sorted(rng.sample(UNICODE_TAGS, k=rng.randint(0, 3))),
        }
        for index in range(size)
    ]


def random_unicode_query(rng: random.Random) -> str:
    tokens = []
    for _ in range(rng.randint(1, 4)):
        word = rng.choice(CASE_TRANSFORMS)(rng.choice(UNICODE_VOCABULARY))
        if rng.random() < 0.3 and len(word) > 2:
            start = rng.randrange(0, len(word) - 1)
            word = word[start : rng.randint(start + 1, len(word))]
        tokens.append(word)
    return "  ".join(tokens)


def oracle_terms(query: str) -> list[str]:
    return list(dict.fromkeys(token.lower() for token in query.split()))


def oracle_matches(entry: dict[str, Any], terms: list[str]) -> bool:
    fields = [
        entry["name"].lower(),
        entry["display_name"].lower(),
        entry["description"].lower(),
        (entry["search_text"] or "").lower(),
    ]
    return all(
        any(term in field for field in fields) or any(term in tag for tag in entry["tags"])
        for term in terms
    )


def oracle_score(entry: dict[str, Any], terms: list[str]) -> int:
    name = entry["name"].lower()
    total = 0
    for term in terms:
        if name == term:
            total += 100
        elif name.startswith(term):
            total += 60
        elif term in name:
            total += 40
        total += 30 if term in entry["display_name"].lower() else 0
        total += 25 if term in entry["tags"] else 0
        total += 10 if term in entry["description"].lower() else 0
        total += 3 if term in (entry["search_text"] or "").lower() else 0
    return total


def oracle_ranking(corpus: list[dict[str, Any]], query: str) -> list[str]:
    terms = oracle_terms(query)
    hits = [entry for entry in corpus if oracle_matches(entry, terms)]
    hits.sort(key=lambda entry: (-oracle_score(entry, terms), entry["name"]))
    return [entry["name"] for entry in hits]


async def search_names(db: AsyncSession, **query: Any) -> list[str]:
    items, _ = await skill_repo.search_skills(db, SkillQuery(limit=100, **query))
    return [item.name for item in items]


def test_escape_like_escapes_wildcards_and_the_escape_character():
    assert escape_like(r"50%_off\now") == r"50\%\_off\\now"
    assert escape_like("plain") == "plain"


def test_split_terms_lowercases_deduplicates_and_caps_the_term_count():
    assert split_terms("  Data   TOOLS data\tfoo\n") == ["data", "tools", "foo"]
    assert split_terms(None) == []
    assert split_terms("   ") == []
    assert split_terms("a\x00b \x00") == ["ab"]
    many = split_terms(" ".join(f"w{i}" for i in range(50)))
    assert many == [f"w{i}" for i in range(skill_repo.MAX_SEARCH_TERMS)]


def test_normalize_tags_strips_lowercases_and_deduplicates_preserving_order():
    assert normalize_tags([" Python ", "docs", "PYTHON", "", "  "]) == ["python", "docs"]


async def test_enums_are_persisted_as_lowercase_values(db, alice, root):
    skill = await add_skill(
        db, alice, "stored", kind=SkillKind.GROUNDING, status=SkillStatus.UNPUBLISHED
    )
    await add_version(db, skill, alice, status=VersionStatus.DEPRECATED)

    roles = (await db.execute(text("SELECT role FROM users ORDER BY username"))).scalars().all()
    kind, status = (await db.execute(text("SELECT kind, status FROM skills"))).one()
    version_status = (await db.execute(text("SELECT status FROM skill_versions"))).scalar_one()

    assert roles == ["publisher", "admin"]
    assert (kind, status, version_status) == ("grounding", "unpublished", "deprecated")


async def test_enum_columns_return_enum_members(db, alice):
    skill = await add_skill(db, alice, "typed", kind=SkillKind.GROUNDING)
    db.expunge_all()

    loaded = await skill_repo.get_skill(db, skill.id)

    assert loaded.kind is SkillKind.GROUNDING
    assert loaded.status is SkillStatus.PUBLISHED


async def test_create_skill_applies_defaults_and_loads_server_generated_values(db, alice):
    skill = await skill_repo.create_skill(
        db,
        name="web-search",
        display_name="Web Search",
        description="Search the web",
        kind=SkillKind.SKILL,
        owner_id=alice.id,
    )

    assert len(skill.id) == 36
    assert skill.status is SkillStatus.DRAFT
    assert skill.latest_version is None
    assert skill.category is None
    assert skill.search_text is None
    assert skill.tags == []
    assert skill.created_at is not None
    assert skill.updated_at is not None


async def test_enum_arguments_may_be_given_as_plain_values_and_are_validated(db, alice):
    skill = await skill_repo.create_skill(
        db,
        name="plain-values",
        display_name="Plain",
        description="",
        kind="grounding",
        owner_id=alice.id,
        status="published",
    )
    version = await add_version(db, skill, alice, status="deprecated")
    await skill_repo.set_skill_status(db, skill, "unpublished")
    assert skill.status is SkillStatus.UNPUBLISHED
    refresh = {
        "display_name": "Plain",
        "description": "",
        "category": None,
        "search_text": None,
        "latest_version": "1.0.0",
        "tags": [],
    }
    await skill_repo.update_skill_denormalized(db, skill, status="published", **refresh)

    assert skill.kind is SkillKind.GROUNDING
    assert version.status is VersionStatus.DEPRECATED
    assert skill.status is SkillStatus.PUBLISHED
    with pytest.raises(ValueError):
        await skill_repo.update_skill_denormalized(
            db, skill, status="archived", **{**refresh, "display_name": "Half applied"}
        )
    assert skill.display_name == "Plain"
    assert skill not in db.dirty
    with pytest.raises(ValueError):
        await skill_repo.create_skill(
            db, name="bad-kind", display_name="B", description="", kind="manual", owner_id=alice.id
        )
    with pytest.raises(ValueError):
        await skill_repo.create_skill(
            db,
            name="bad-status",
            display_name="B",
            description="",
            kind="skill",
            owner_id=alice.id,
            status="archived",
        )
    with pytest.raises(ValueError):
        await add_version(db, skill, alice, "2.0.0", status="archived")
    with pytest.raises(ValueError):
        await skill_repo.set_skill_status(db, skill, "archived")


async def test_create_skill_normalizes_and_deduplicates_tags(db, alice):
    skill = await add_skill(db, alice, "tagged", tags=["Python", " python ", "docs", ""])

    assert skill.tags == ["docs", "python"]


async def test_model_defaults_apply_to_rows_built_without_the_repository(db, alice):
    skill = Skill(name="bare", display_name="Bare", owner_id=alice.id)
    db.add(skill)
    await db.flush()
    version = SkillVersion(
        skill_id=skill.id,
        version="1.0.0",
        blob_sha256="a" * 64,
        size_bytes=1,
        uncompressed_bytes=1,
        file_count=1,
        entrypoint="SKILL.md",
        display_name="Bare",
        uploaded_by=alice.id,
    )
    db.add(version)
    await db.flush()

    assert skill.kind is SkillKind.SKILL
    assert skill.status is SkillStatus.DRAFT
    assert skill.description == ""
    assert version.status is VersionStatus.DRAFT
    assert (version.tags, version.metadata_, version.manifest) == ("[]", "{}", "[]")
    assert (version.search_text, version.root_prefix, version.description) == ("", "", "")


async def test_get_skill_by_id_and_by_name(db, alice):
    skill = await add_skill(db, alice, "pdf-tools")
    db.expunge_all()

    by_id = await skill_repo.get_skill(db, skill.id)
    by_name = await skill_repo.get_skill_by_name(db, "pdf-tools")

    assert by_id is by_name
    assert by_id.name == "pdf-tools"
    assert await skill_repo.get_skill(db, "missing") is None
    assert await skill_repo.get_skill_by_name(db, "PDF-TOOLS") is None
    assert await skill_repo.get_skill_by_name(db, "missing") is None


async def test_lock_skill_returns_the_locked_entry_and_none_for_unknown_ids(db, alice):
    skill = await add_skill(db, alice, "lockable", tags=["x"])

    assert await skill_repo.lock_skill(db, skill.id) is skill
    assert await skill_repo.lock_skill(db, "missing") is None


async def test_lock_skill_refreshes_an_instance_that_went_stale_in_the_session(db, alice):
    skill = await add_skill(db, alice, "stale-entry", tags=["old"])
    await db.commit()
    await db.execute(
        text("UPDATE skills SET status = 'unpublished', latest_version = '9.9.9' WHERE id = :id"),
        {"id": skill.id},
    )
    await db.execute(
        text("INSERT INTO skill_tags (skill_id, tag) VALUES (:id, 'fresh')"), {"id": skill.id}
    )
    assert skill.status is SkillStatus.PUBLISHED
    assert skill.tags == ["old"]

    locked = await skill_repo.lock_skill(db, skill.id)

    assert locked is skill
    assert (skill.status, skill.latest_version) == (SkillStatus.UNPUBLISHED, "9.9.9")
    assert skill.tags == ["fresh", "old"]


async def test_lock_skill_asks_postgresql_for_a_row_lock(db, alice):
    skill = await add_skill(db, alice, "row-lock")
    statements = []

    @event.listens_for(db.sync_session, "do_orm_execute")
    def capture(state):
        statements.append(str(state.statement.compile(dialect=postgresql.dialect())))

    await skill_repo.lock_skill(db, skill.id)

    locking = [sql for sql in statements if "FOR UPDATE" in sql]
    assert len(locking) == 1
    assert "FROM skills" in locking[0] and "skills.id = %(id_1)s" in locking[0]


async def test_duplicate_skill_name_is_rejected(db, alice, bob):
    await add_skill(db, alice, "taken")

    with pytest.raises(IntegrityError):
        await add_skill(db, bob, "taken")


async def test_repository_writes_flush_but_never_commit(db, alice):
    await db.commit()
    skill = await add_skill(db, alice, "ephemeral", tags=["x"])
    await add_version(db, skill, alice)
    assert await skill_repo.get_skill_by_name(db, "ephemeral") is not None

    await db.rollback()

    assert await skill_repo.get_skill_by_name(db, "ephemeral") is None
    assert (await db.execute(select(SkillTag))).first() is None
    assert (await db.execute(text("SELECT count(*) FROM skill_versions"))).scalar_one() == 0


async def test_repository_updates_flush_but_never_commit(db, alice):
    skill = await add_skill(
        db,
        alice,
        "stable",
        description="before",
        tags=["old"],
        status=SkillStatus.DRAFT,
        latest_version=None,
    )
    version = await add_version(db, skill, alice)
    await db.commit()

    await skill_repo.update_skill_denormalized(
        db,
        skill,
        display_name="After",
        description="after",
        category="changed",
        search_text="changed",
        latest_version="9.9.9",
        tags=["new"],
        status=SkillStatus.PUBLISHED,
    )
    await skill_repo.set_skill_status(db, skill, SkillStatus.UNPUBLISHED)
    await skill_repo.update_version(db, version, status=VersionStatus.PUBLISHED)
    await db.rollback()

    stored_skill = (
        await db.execute(text("SELECT description, status, latest_version FROM skills"))
    ).one()
    stored_version = (await db.execute(text("SELECT status FROM skill_versions"))).scalar_one()
    stored_tags = (await db.execute(text("SELECT tag FROM skill_tags"))).scalars().all()
    assert tuple(stored_skill) == ("before", "draft", None)
    assert stored_version == "draft"
    assert stored_tags == ["old"]


async def test_update_denormalized_replaces_fields_and_tag_rows(db, alice):
    skill = await add_skill(
        db,
        alice,
        "evolving",
        display_name="Old",
        description="old",
        category="old",
        search_text="old",
        tags=["a", "b"],
        status=SkillStatus.DRAFT,
        latest_version=None,
    )
    skill.updated_at = datetime(2020, 1, 1)
    await db.flush()

    updated = await skill_repo.update_skill_denormalized(
        db,
        skill,
        display_name="New",
        description="new",
        category="fresh",
        search_text="new body",
        latest_version="2.0.0",
        tags=["b", "c", "B"],
        status=SkillStatus.PUBLISHED,
    )

    assert updated is skill
    assert skill.display_name == "New"
    assert skill.description == "new"
    assert skill.category == "fresh"
    assert skill.search_text == "new body"
    assert skill.latest_version == "2.0.0"
    assert skill.status is SkillStatus.PUBLISHED
    assert skill.tags == ["b", "c"]
    assert skill.updated_at > datetime(2021, 1, 1)
    stored = (
        (await db.execute(select(SkillTag.tag).where(SkillTag.skill_id == skill.id)))
        .scalars()
        .all()
    )
    assert sorted(stored) == ["b", "c"]


async def test_update_denormalized_keeps_status_when_not_given_and_can_clear_values(db, alice):
    skill = await add_skill(
        db,
        alice,
        "clearable",
        category="ops",
        search_text="body",
        tags=["a"],
        status=SkillStatus.UNPUBLISHED,
    )

    await skill_repo.update_skill_denormalized(
        db,
        skill,
        display_name="Clearable",
        description="",
        category=None,
        search_text=None,
        latest_version=None,
        tags=[],
    )

    assert skill.status is SkillStatus.UNPUBLISHED
    assert skill.category is None
    assert skill.search_text is None
    assert skill.latest_version is None
    assert skill.tags == []
    assert (await db.execute(select(SkillTag))).first() is None


async def test_update_denormalized_touches_updated_at_even_when_only_tags_change(db, alice):
    skill = await add_skill(db, alice, "retagged", display_name="Same", description="same")
    skill.updated_at = datetime(2020, 1, 1)
    await db.flush()

    await skill_repo.update_skill_denormalized(
        db,
        skill,
        display_name="Same",
        description="same",
        category=None,
        search_text=None,
        latest_version="1.0.0",
        tags=["fresh"],
    )

    assert skill.updated_at > datetime(2021, 1, 1)
    assert skill.tags == ["fresh"]


async def test_update_denormalized_survives_a_fresh_session_load(db, alice):
    skill = await add_skill(db, alice, "persisted", tags=["a"])
    await skill_repo.update_skill_denormalized(
        db,
        skill,
        display_name="P",
        description="d",
        category=None,
        search_text=None,
        latest_version="1.0.0",
        tags=["z", "y"],
    )
    await db.commit()
    db.expunge_all()

    reloaded = await skill_repo.get_skill_by_name(db, "persisted")

    assert reloaded.tags == ["y", "z"]
    assert reloaded.display_name == "P"


async def test_set_skill_status_changes_status_and_touches_updated_at(db, alice):
    skill = await add_skill(db, alice, "hideable")
    skill.updated_at = datetime(2020, 1, 1)
    await db.flush()

    result = await skill_repo.set_skill_status(db, skill, SkillStatus.UNPUBLISHED)

    assert result is skill
    assert skill.status is SkillStatus.UNPUBLISHED
    assert skill.updated_at > datetime(2021, 1, 1)


async def test_deleting_a_skill_through_the_orm_removes_its_tag_rows(db, alice):
    skill = await add_skill(db, alice, "doomed", tags=["x", "y"])

    await db.delete(skill)
    await db.flush()

    assert (await db.execute(select(SkillTag))).first() is None
    assert await skill_repo.get_skill_by_name(db, "doomed") is None


async def test_database_cascades_tag_rows_when_a_skill_row_is_deleted(db, alice):
    skill = await add_skill(db, alice, "doomed", tags=["x", "y"])
    await add_skill(db, alice, "survivor", tags=["z"])

    await db.execute(text("DELETE FROM skills WHERE id = :id"), {"id": skill.id})

    remaining = (await db.execute(text("SELECT tag FROM skill_tags"))).scalars().all()
    assert remaining == ["z"]


async def test_database_refuses_to_delete_a_skill_that_still_has_versions(db, alice):
    skill = await add_skill(db, alice, "protected")
    await add_version(db, skill, alice)

    with pytest.raises(IntegrityError):
        await db.execute(text("DELETE FROM skills WHERE id = :id"), {"id": skill.id})


async def test_create_version_serializes_json_columns_and_applies_defaults(db, alice):
    skill = await add_skill(db, alice, "packaged")
    manifest = [
        {
            "path": "SKILL.md",
            "size": 5,
            "sha256": "f" * 64,
            "media_type": "text/markdown",
            "text": True,
        }
    ]

    version = await add_version(
        db,
        skill,
        alice,
        "1.2.3",
        tags=["a", "b"],
        metadata={"author": "ñandú", "n": 1},
        manifest=manifest,
        root_prefix="pkg/",
        category="ops",
        search_text="body",
    )

    assert version.status is VersionStatus.DRAFT
    assert json.loads(version.tags) == ["a", "b"]
    assert json.loads(version.metadata_) == {"author": "ñandú", "n": 1}
    assert "ñandú" in version.metadata_
    assert json.loads(version.manifest) == manifest
    assert list(json.loads(version.manifest)[0]) == list(manifest[0])
    assert version.root_prefix == "pkg/"
    assert version.category == "ops"
    assert version.search_text == "body"
    assert version.blob_sha256 == "a" * 64
    assert (version.size_bytes, version.uncompressed_bytes, version.file_count) == (100, 200, 2)
    assert version.entrypoint == "SKILL.md"
    assert version.uploaded_by == alice.id
    assert version.published_by is None
    assert version.published_at is None
    assert version.deprecated_at is None
    assert version.created_at is not None


async def test_create_version_defaults_for_optional_json_values(db, alice):
    skill = await add_skill(db, alice, "bare")

    version = await add_version(db, skill, alice)

    assert version.tags == "[]"
    assert version.metadata_ == "{}"
    assert version.manifest == "[]"
    assert version.search_text == ""
    assert version.root_prefix == ""
    assert version.category is None


async def test_create_version_can_be_published_in_the_same_call(db, alice, bob):
    skill = await add_skill(db, alice, "instant")
    moment = datetime(2025, 3, 4, 5, 6, 7)

    version = await add_version(
        db, skill, alice, status=VersionStatus.PUBLISHED, published_by=bob.id, published_at=moment
    )

    assert version.status is VersionStatus.PUBLISHED
    assert version.published_by == bob.id
    assert version.published_at == moment


async def test_version_numbers_are_unique_per_skill_but_not_across_skills(db, alice):
    first = await add_skill(db, alice, "first")
    second = await add_skill(db, alice, "second")
    await add_version(db, first, alice, "1.0.0")
    await add_version(db, second, alice, "1.0.0")

    with pytest.raises(IntegrityError):
        await add_version(db, first, alice, "1.0.0")


async def test_versions_require_existing_skill_and_uploader(db, alice):
    skill = await add_skill(db, alice, "linked")
    ghost = User(id="ghost-user-id", username="ghost", email="g@x.io", password_hash="x")

    with pytest.raises(IntegrityError):
        await add_version(db, skill, ghost)


async def test_get_version_by_number_and_by_id(db, alice):
    skill = await add_skill(db, alice, "lookup")
    other = await add_skill(db, alice, "other")
    version = await add_version(db, skill, alice, "1.0.0")
    db.expunge_all()

    by_number = await skill_repo.get_version(db, skill.id, "1.0.0")
    by_id = await skill_repo.get_version_by_id(db, version.id)

    assert by_number is by_id
    assert by_id.version == "1.0.0"
    assert await skill_repo.get_version(db, skill.id, "9.9.9") is None
    assert await skill_repo.get_version(db, other.id, "1.0.0") is None
    assert await skill_repo.get_version_by_id(db, "missing") is None


async def test_list_versions_is_scoped_to_the_skill_and_filterable_by_status(db, alice):
    skill = await add_skill(db, alice, "versioned")
    other = await add_skill(db, alice, "unrelated")
    await add_version(db, skill, alice, "1.0.0", status=VersionStatus.DEPRECATED)
    await add_version(db, skill, alice, "1.1.0", status=VersionStatus.PUBLISHED)
    await add_version(db, skill, alice, "2.0.0", status=VersionStatus.DRAFT)
    await add_version(db, other, alice, "3.0.0")

    everything = await skill_repo.list_versions(db, skill.id)
    readable = await skill_repo.list_versions(
        db, skill.id, statuses=[VersionStatus.PUBLISHED, VersionStatus.DEPRECATED]
    )
    drafts = await skill_repo.list_versions(db, skill.id, statuses=[VersionStatus.DRAFT])
    nothing = await skill_repo.list_versions(db, skill.id, statuses=[])

    assert {v.version for v in everything} == {"1.0.0", "1.1.0", "2.0.0"}
    assert {v.version for v in readable} == {"1.0.0", "1.1.0"}
    assert [v.version for v in drafts] == ["2.0.0"]
    assert nothing == []
    assert await skill_repo.list_versions(db, "missing") == []


async def test_list_versions_returns_fresh_rows_for_instances_already_loaded(db, alice):
    skill = await add_skill(db, alice, "fresh-rows")
    first = await add_version(db, skill, alice, "1.0.0", status=VersionStatus.PUBLISHED)
    second = await add_version(db, skill, alice, "1.1.0", status=VersionStatus.PUBLISHED)
    await db.commit()
    published = (VersionStatus.PUBLISHED,)
    assert {v.id for v in await skill_repo.list_versions(db, skill.id, statuses=published)} == {
        first.id,
        second.id,
    }
    await db.execute(
        text("UPDATE skill_versions SET status = 'deprecated' WHERE id = :id"), {"id": first.id}
    )
    plain = (
        await db.execute(select(SkillVersion).where(SkillVersion.skill_id == skill.id))
    ).scalars()
    assert {v.status for v in plain} == {VersionStatus.PUBLISHED}

    everything = await skill_repo.list_versions(db, skill.id)
    only_published = await skill_repo.list_versions(db, skill.id, statuses=published)

    assert {v.version: v.status for v in everything} == {
        "1.0.0": VersionStatus.DEPRECATED,
        "1.1.0": VersionStatus.PUBLISHED,
    }
    assert [v.id for v in only_published] == [second.id]
    assert next(v for v in everything if v.id == first.id) is first


HEAVY_COLUMNS = ("manifest", "metadata", "search_text")
HEAVY_ATTRIBUTES = frozenset({"manifest", "metadata_", "search_text"})
LIGHT_COLUMNS = (
    "id",
    "skill_id",
    "version",
    "status",
    "blob_sha256",
    "size_bytes",
    "uncompressed_bytes",
    "file_count",
    "entrypoint",
    "root_prefix",
    "display_name",
    "description",
    "category",
    "tags",
    "uploaded_by",
    "published_by",
    "published_at",
    "deprecated_at",
    "created_at",
)
FAT_MANIFEST = [
    {
        "path": f"docs/file-{index:04d}.md",
        "size": index,
        "sha256": f"{index:064x}",
        "media_type": "text/markdown",
        "text": True,
    }
    for index in range(300)
]


def selected_columns(statement: str) -> str:
    return " ".join(statement.split()).split(" FROM ")[0]


async def add_fat_version(db, skill, uploader, version, **overrides):
    values = {
        "manifest": FAT_MANIFEST,
        "metadata": {"notes": "n" * 2000},
        "search_text": f"body of {version} " * 500,
    }
    values.update(overrides)
    return await add_version(db, skill, uploader, version, **values)


async def test_list_versions_never_selects_the_heavy_columns(engine, db, alice):
    skill = await add_skill(db, alice, "fat")
    for number in ("1.0.0", "1.1.0", "2.0.0"):
        await add_fat_version(db, skill, alice, number)
    await db.commit()
    db.expunge_all()

    with record_statements(engine) as statements:
        listed = await skill_repo.list_versions(db, skill.id)
        published = await skill_repo.list_versions(
            db, skill.id, statuses=[VersionStatus.PUBLISHED, VersionStatus.DEPRECATED]
        )

    assert sorted(item.version for item in listed) == ["1.0.0", "1.1.0", "2.0.0"]
    assert published == []
    assert len(statements) == 2
    for statement, _ in statements:
        selected = selected_columns(statement)
        assert not any(f"skill_versions.{column}" in selected for column in HEAVY_COLUMNS)
        assert all(f"skill_versions.{column}" in selected for column in LIGHT_COLUMNS)


async def test_list_versions_leaves_the_heavy_attributes_unloaded_and_the_rest_ready(
    engine, db, alice
):
    skill = await add_skill(db, alice, "deferred")
    await add_fat_version(db, skill, alice, "1.0.0", tags=["a"], category="ops")
    await db.commit()
    db.expunge_all()

    (listed,) = await skill_repo.list_versions(db, skill.id)

    unloaded = inspect(listed).unloaded
    assert HEAVY_ATTRIBUTES <= unloaded
    assert not {"id", "version", "status", "tags", "display_name", "description"} & unloaded
    with record_statements(engine) as statements:
        assert json.loads(listed.tags) == ["a"]
        assert (listed.version, listed.category, listed.status) == ("1.0.0", "ops", listed.status)
        assert listed.created_at is not None
    assert statements == []


async def test_a_deferred_attribute_needs_an_explicit_query_and_get_version_provides_it(db, alice):
    skill = await add_skill(db, alice, "explicit")
    await add_fat_version(db, skill, alice, "1.0.0")
    await db.commit()
    db.expunge_all()
    (listed,) = await skill_repo.list_versions(db, skill.id)

    assert HEAVY_ATTRIBUTES <= inspect(listed).unloaded

    full = await skill_repo.get_version(db, skill.id, "1.0.0")
    assert full is listed
    assert not HEAVY_ATTRIBUTES & inspect(full).unloaded
    assert json.loads(full.manifest) == FAT_MANIFEST
    assert full.search_text.startswith("body of 1.0.0")
    assert json.loads(full.metadata_) == {"notes": "n" * 2000}


async def test_list_versions_returns_the_same_light_values_as_a_full_load(db, alice, bob):
    skill = await add_skill(db, alice, "same-values")
    await add_fat_version(db, skill, alice, "1.0.0", tags=["x", "y"], category="ops")
    await add_fat_version(
        db, skill, bob, "1.1.0", status=VersionStatus.PUBLISHED, published_by=alice.id
    )
    await db.commit()
    db.expunge_all()
    light = await skill_repo.list_versions(db, skill.id)
    light_values = [{column: getattr(item, column) for column in LIGHT_COLUMNS} for item in light]
    db.expunge_all()

    full = list(
        (
            await db.execute(
                select(SkillVersion)
                .where(SkillVersion.skill_id == skill.id)
                .order_by(SkillVersion.created_at, SkillVersion.id)
            )
        ).scalars()
    )

    assert light_values == [
        {column: getattr(item, column) for column in LIGHT_COLUMNS} for item in full
    ]


async def test_get_version_search_text_reads_only_that_column_of_that_row(engine, db, alice):
    skill = await add_skill(db, alice, "texts")
    first = await add_fat_version(db, skill, alice, "1.0.0", search_text="first body")
    await add_fat_version(db, skill, alice, "1.1.0", search_text="second body")
    await db.commit()
    db.expunge_all()

    with record_statements(engine) as statements:
        found = await skill_repo.get_version_search_text(db, first.id)

    assert found == "first body"
    assert len(statements) == 1
    statement, parameters = statements[0]
    assert selected_columns(statement) == "SELECT skill_versions.search_text"
    assert "skill_versions.id = ?" in statement
    assert parameters == (first.id,)


async def test_update_version_applies_lifecycle_transitions(db, alice, bob):
    skill = await add_skill(db, alice, "lifecycle")
    version = await add_version(db, skill, alice)
    published_at = datetime(2025, 5, 1, 12, 0, 0)
    deprecated_at = datetime(2025, 6, 1, 12, 0, 0)

    await skill_repo.update_version(
        db, version, status=VersionStatus.PUBLISHED, published_by=bob.id, published_at=published_at
    )
    assert version.status is VersionStatus.PUBLISHED
    assert version.published_by == bob.id
    assert version.published_at == published_at

    await skill_repo.update_version(
        db, version, status=VersionStatus.DEPRECATED, deprecated_at=deprecated_at
    )
    assert version.status is VersionStatus.DEPRECATED
    assert version.deprecated_at == deprecated_at
    assert version.published_at == published_at

    await skill_repo.update_version(db, version, status=VersionStatus.PUBLISHED, deprecated_at=None)
    assert version.status is VersionStatus.PUBLISHED
    assert version.deprecated_at is None
    assert version.published_by == bob.id
    assert version.published_at == published_at


async def test_update_version_coerces_status_values(db, alice):
    skill = await add_skill(db, alice, "coerced")
    version = await add_version(db, skill, alice)

    await skill_repo.update_version(db, version, status="published")

    assert version.status is VersionStatus.PUBLISHED
    with pytest.raises(ValueError):
        await skill_repo.update_version(db, version, status="archived")


IMMUTABLE_VERSION_FIELDS = [
    "id",
    "skill_id",
    "version",
    "blob_sha256",
    "size_bytes",
    "uncompressed_bytes",
    "file_count",
    "entrypoint",
    "root_prefix",
    "display_name",
    "description",
    "category",
    "tags",
    "metadata_",
    "manifest",
    "search_text",
    "uploaded_by",
    "created_at",
]


@pytest.mark.parametrize("field", IMMUTABLE_VERSION_FIELDS)
async def test_update_version_rejects_every_immutable_field(db, alice, field):
    skill = await add_skill(db, alice, "sealed")
    version = await add_version(db, skill, alice, manifest=[{"path": "SKILL.md"}])
    before_row = (
        await db.execute(text("SELECT * FROM skill_versions WHERE id = :id"), {"id": version.id})
    ).one()
    before_value = getattr(version, field)

    with pytest.raises(ValueError, match=field):
        await skill_repo.update_version(db, version, **{field: "tampered"})

    after_row = (
        await db.execute(text("SELECT * FROM skill_versions WHERE id = :id"), {"id": version.id})
    ).one()
    assert after_row == before_row
    assert getattr(version, field) == before_value


async def test_update_version_validates_before_applying_anything(db, alice):
    skill = await add_skill(db, alice, "atomic-update")
    version = await add_version(db, skill, alice)

    with pytest.raises(ValueError):
        await skill_repo.update_version(db, version, status=VersionStatus.PUBLISHED, manifest="[]")

    assert version.status is VersionStatus.DRAFT
    stored = (await db.execute(text("SELECT status FROM skill_versions"))).scalar_one()
    assert stored == "draft"


async def test_count_versions_by_status(db, alice):
    skill = await add_skill(db, alice, "counted")
    other = await add_skill(db, alice, "uncounted")
    await add_version(db, skill, alice, "1.0.0", status=VersionStatus.DRAFT)
    await add_version(db, skill, alice, "1.1.0", status=VersionStatus.DRAFT)
    await add_version(db, skill, alice, "2.0.0", status=VersionStatus.PUBLISHED)
    await add_version(db, other, alice, "1.0.0", status=VersionStatus.DEPRECATED)

    counts = await skill_repo.count_versions_by_status(db, skill.id)

    assert counts == {
        VersionStatus.DRAFT: 2,
        VersionStatus.PUBLISHED: 1,
        VersionStatus.DEPRECATED: 0,
    }
    assert await skill_repo.count_versions_by_status(db, "missing") == dict.fromkeys(
        VersionStatus, 0
    )


async def test_search_without_filters_lists_visible_entries_by_name(db, alice):
    for name in ("charlie", "alpha", "bravo"):
        await add_skill(db, alice, name)

    items, total = await skill_repo.search_skills(db, SkillQuery())

    assert [item.name for item in items] == ["alpha", "bravo", "charlie"]
    assert total == 3
    assert (await skill_repo.search_skills(db, SkillQuery(q=" \t ")))[1] == 3


async def test_search_on_an_empty_registry(db):
    assert await skill_repo.search_skills(db, SkillQuery(q="anything")) == ([], 0)
    assert await skill_repo.search_skills(db, SkillQuery()) == ([], 0)


async def test_search_matches_every_searchable_field_case_insensitively(db, alice):
    await add_skill(db, alice, "by-name-needle")
    await add_skill(db, alice, "c1", display_name="Has A NEEDLE Inside")
    await add_skill(db, alice, "c2", description="describes the Needle here")
    await add_skill(db, alice, "c3", tags=["needle"])
    await add_skill(db, alice, "c4", search_text="long body mentioning NeEdLe somewhere")
    await add_skill(db, alice, "unrelated", description="nothing to see")

    expected = ["by-name-needle", "c1", "c2", "c3", "c4"]
    for term in ("needle", "NEEDLE", "NeEdLe"):
        assert sorted(await search_names(db, q=term)) == expected


async def test_search_matches_tags_by_substring(db, alice):
    await add_skill(db, alice, "pythonic", tags=["python"])
    await add_skill(db, alice, "other", tags=["rust"])

    assert await search_names(db, q="pyth") == ["pythonic"]


async def test_search_folds_the_case_of_non_ascii_letters_in_every_searchable_field(db, alice):
    await add_skill(db, alice, "by-display", display_name="Índice ÚTIL Über")
    await add_skill(db, alice, "by-description", description="Árbol de decisión para ÉCOLE")
    await add_skill(db, alice, "by-body", search_text="Ñandú ÁRBOL MÉTODO")
    await add_skill(db, alice, "unrelated", description="nothing to see here")

    expected = {
        "índice": ["by-display"],
        "ÍNDICE": ["by-display"],
        "Índice": ["by-display"],
        "útil": ["by-display"],
        "ÜBER": ["by-display"],
        "árbol": ["by-body", "by-description"],
        "ÁRBOL": ["by-body", "by-description"],
        "Árbol": ["by-body", "by-description"],
        "árbol de": ["by-description"],
        "ÁRBOL DE DECISIÓN": ["by-description"],
        "école": ["by-description"],
        "ÉCOLE": ["by-description"],
        "ñandú": ["by-body"],
        "ÑANDÚ": ["by-body"],
        "método": ["by-body"],
        "MÉTODO": ["by-body"],
        "decisión": ["by-description"],
        "árbol método": ["by-body"],
        "árbol útil": [],
        "école ñandú": [],
    }
    for term, names in expected.items():
        assert sorted(await search_names(db, q=term)) == names, term
        _, total = await skill_repo.search_skills(db, SkillQuery(q=term))
        assert total == len(names), term


async def test_search_ranks_non_ascii_matches_by_the_documented_field_weights(db, alice):
    await add_skill(
        db, alice, "w-body", display_name="Zulu", description="x", search_text="el árbol"
    )
    await add_skill(
        db, alice, "w-description", display_name="Yankee", description="un ÁRBOL grande"
    )
    await add_skill(db, alice, "w-display", display_name="Árbol", description="x")
    await add_skill(
        db, alice, "w-all", display_name="ÁRBOL", description="Árbol", search_text="árbol"
    )
    await add_skill(db, alice, "w-none", display_name="Xray", description="x")

    assert await search_names(db, q="ÁRBOL") == ["w-all", "w-display", "w-description", "w-body"]
    assert await search_names(db, q="árbol", sort="name") == [
        "w-all",
        "w-body",
        "w-description",
        "w-display",
    ]


async def test_search_matches_non_ascii_text_against_the_python_oracle(db, alice):
    rng = random.Random(20260401)
    corpus = unicode_corpus(rng, 80)
    for entry in corpus:
        await add_skill(
            db,
            alice,
            entry["name"],
            display_name=entry["display_name"],
            description=entry["description"],
            search_text=entry["search_text"],
            tags=entry["tags"],
        )
    queries = [random_unicode_query(rng) for _ in range(300)]

    mismatches = []
    answered = 0
    for query in queries:
        expected = oracle_ranking(corpus, query)
        items, total = await skill_repo.search_skills(db, SkillQuery(q=query, limit=100))
        answered += bool(expected)
        if [item.name for item in items] != expected or total != len(expected):
            mismatches.append((query, expected[:5], [item.name for item in items][:5]))

    assert mismatches == []
    assert 150 < answered < len(queries) - 20


async def test_search_requires_every_term_but_each_may_match_a_different_field(db, alice):
    await add_skill(db, alice, "pdf-tools", description="extract text")
    await add_skill(db, alice, "pdf-only", description="nothing relevant")
    await add_skill(db, alice, "tools-only", description="extract text")
    await add_skill(db, alice, "docs", tags=["pdf"], description="tools for text")

    assert sorted(await search_names(db, q="pdf tools")) == ["docs", "pdf-tools"]
    assert sorted(await search_names(db, q="  PDF   Tools  ")) == ["docs", "pdf-tools"]
    assert sorted(await search_names(db, q="tools pdf")) == ["docs", "pdf-tools"]
    assert await search_names(db, q="pdf tools missing") == []
    assert sorted(await search_names(db, q="pdf pdf PDF")) == ["docs", "pdf-only", "pdf-tools"]


async def test_search_ranks_by_the_documented_field_weights(db, alice):
    await add_skill(db, alice, "victor", display_name="Golf", tags=["dataset"])
    await add_skill(db, alice, "whiskey", display_name="Foxtrot", search_text="uses data inside")
    await add_skill(db, alice, "xray", display_name="Echo", description="handles data pipelines")
    await add_skill(db, alice, "yankee", display_name="Delta", tags=["data"])
    await add_skill(db, alice, "zulu", display_name="Big Data Zone")
    await add_skill(db, alice, "big-data", display_name="Charlie")
    await add_skill(db, alice, "data-tools", display_name="Bravo")
    await add_skill(db, alice, "data", display_name="Alpha")
    await add_skill(db, alice, "noise", display_name="Hotel", description="unrelated")

    ranked = await search_names(db, q="data")

    assert ranked == [
        "data",
        "data-tools",
        "big-data",
        "zulu",
        "yankee",
        "xray",
        "whiskey",
        "victor",
    ]


async def test_search_scores_are_summed_across_fields(db, alice):
    await add_skill(db, alice, "data", display_name="Alpha")
    await add_skill(
        db,
        alice,
        "big-data",
        display_name="Big Data",
        description="data",
        tags=["data"],
        search_text="data",
    )

    assert await search_names(db, q="data") == ["big-data", "data"]


async def test_search_scores_are_summed_across_terms(db, alice):
    await add_skill(db, alice, "pdf-tools", display_name="Zed A")
    await add_skill(db, alice, "pdf", display_name="Zed B", description="tools for docs")
    await add_skill(db, alice, "docs", display_name="Zed D", tags=["pdf"], description="tools")
    await add_skill(db, alice, "pdf-only", display_name="Zed C")

    assert await search_names(db, q="pdf tools") == ["pdf", "pdf-tools", "docs"]


async def test_search_breaks_score_ties_by_name(db, alice):
    for name in ("delta", "alpha", "charlie", "bravo"):
        await add_skill(db, alice, name, display_name="Same", description="shared phrase")

    assert await search_names(db, q="phrase") == ["alpha", "bravo", "charlie", "delta"]


async def test_search_treats_like_wildcards_as_literal_characters(db, alice):
    await add_skill(db, alice, "percent", description="Handles 100% of cases")
    await add_skill(db, alice, "thousand", description="Handles 1000 cases")
    await add_skill(db, alice, "snake", description="uses snake_case naming")
    await add_skill(db, alice, "camel", description="uses snakecase naming")
    await add_skill(db, alice, "windows", description=r"stores files in C:\temp")
    await add_skill(db, alice, "plain", description="nothing special")
    await add_skill(db, alice, "tail", search_text="ends with a backslash \\")
    await add_skill(db, alice, "combo", description=r"literal 5\%_ sequence")

    async def found(q: str) -> list[str]:
        return sorted(await search_names(db, q=q))

    assert await found("100%") == ["percent"]
    assert await found("%") == ["combo", "percent"]
    assert await found("_") == ["combo", "snake"]
    assert await found("snake_case") == ["snake"]
    assert await found("e_c") == ["snake"]
    assert await found("n_k") == []
    assert await found("\\") == ["combo", "tail", "windows"]
    assert await found("c:\\temp") == ["windows"]
    assert await found("5\\%_") == ["combo"]
    assert await found("%%") == []
    assert await found("__") == []


async def test_search_wildcards_in_tags_and_names_are_literal_too(db, alice):
    await add_skill(db, alice, "has-tag", tags=["c++", "a.b"])
    await add_skill(db, alice, "other", tags=["cxx", "axb"])

    assert await search_names(db, q="a.b") == ["has-tag"]
    assert await search_names(db, q="%") == []
    assert await search_names(db, q="_") == []


async def test_search_treats_hostile_input_as_data(db, alice):
    await add_skill(db, alice, "safe", description="harmless", tags=["fine"], category="ops")
    hostile = [
        "'; DROP TABLE skills; --",
        '" OR 1=1 --',
        "%' OR '1'='1",
        "x') OR ('1'='1",
        "\x00",
    ]

    for payload in hostile[:-1]:
        assert await skill_repo.search_skills(db, SkillQuery(q=payload)) == ([], 0)
        assert await skill_repo.search_skills(db, SkillQuery(tags=[payload])) == ([], 0)
        assert await skill_repo.search_skills(db, SkillQuery(category=payload)) == ([], 0)
        assert await skill_repo.search_skills(db, SkillQuery(owner_id=payload)) == ([], 0)

    assert await search_names(db, q=hostile[-1]) == ["safe"]
    assert await skill_repo.get_skill_by_name(db, "safe") is not None


async def test_search_strips_nul_characters_from_terms(db, alice):
    await add_skill(db, alice, "cabinet-maker", description="builds a cabinet")

    assert await search_names(db, q="a\x00b") == ["cabinet-maker"]


async def test_search_survives_absurdly_long_queries(db, alice):
    await add_skill(db, alice, "target", description="t0 t1 t2 t3 t4 t5 t6 t7 t8 t9")

    huge = " ".join(f"w{i}" for i in range(2000))
    assert await skill_repo.search_skills(db, SkillQuery(q=huge)) == ([], 0)

    capped = " ".join(f"t{i}" for i in range(10)) + " " + " ".join(f"x{i}" for i in range(500))
    assert await search_names(db, q=capped) == ["target"]


async def test_search_filters_by_kind(db, alice):
    await add_skill(db, alice, "how-to", kind=SkillKind.SKILL)
    await add_skill(db, alice, "reference", kind=SkillKind.GROUNDING)

    assert await search_names(db, kind=SkillKind.SKILL) == ["how-to"]
    assert await search_names(db, kind=SkillKind.GROUNDING) == ["reference"]
    assert await search_names(db, kind="grounding") == ["reference"]
    assert await search_names(db) == ["how-to", "reference"]
    with pytest.raises(ValueError):
        await skill_repo.search_skills(db, SkillQuery(kind="manual"))


async def test_search_filters_by_exact_category(db, alice):
    await add_skill(db, alice, "ops-one", category="ops")
    await add_skill(db, alice, "ops-two", category="ops-extra")
    await add_skill(db, alice, "dev-one", category="dev")
    await add_skill(db, alice, "uncategorized")

    assert await search_names(db, category="ops") == ["ops-one"]
    assert await search_names(db, category=" OPS ") == ["ops-one"]
    assert await search_names(db, category="op") == []
    assert await search_names(db, category="") == [
        "dev-one",
        "ops-one",
        "ops-two",
        "uncategorized",
    ]


async def test_search_combines_multiple_tags_with_and(db, alice):
    await add_skill(db, alice, "both", tags=["python", "docs"])
    await add_skill(db, alice, "only-python", tags=["python"])
    await add_skill(db, alice, "only-docs", tags=["docs", "extra"])
    await add_skill(db, alice, "none")

    assert await search_names(db, tags=["python"]) == ["both", "only-python"]
    assert await search_names(db, tags=["python", "docs"]) == ["both"]
    assert await search_names(db, tags=[" Python ", "DOCS", "python"]) == ["both"]
    assert await search_names(db, tags=["python", "docs", "missing"]) == []
    assert await search_names(db, tags=[]) == ["both", "none", "only-docs", "only-python"]
    assert await search_names(db, tags=["python"], q="only") == ["only-python"]


async def test_search_tag_filter_is_exact_not_substring(db, alice):
    await add_skill(db, alice, "pythonic", tags=["python"])

    assert await search_names(db, tags=["pyth"]) == []
    assert await search_names(db, tags=["python3"]) == []


async def test_search_never_matches_more_tags_than_an_entry_may_have(db, alice):
    twenty = [f"t{i}" for i in range(20)]
    await add_skill(db, alice, "maxed", tags=twenty)

    assert await search_names(db, tags=twenty) == ["maxed"]
    assert await skill_repo.search_skills(db, SkillQuery(tags=twenty + ["t20"])) == ([], 0)
    thousands = [f"t{i}" for i in range(3000)]
    assert await skill_repo.search_skills(db, SkillQuery(tags=thousands)) == ([], 0)


async def test_search_combines_filters_with_query_terms(db, alice):
    await add_skill(db, alice, "ops-report", kind=SkillKind.SKILL, category="ops", tags=["x"])
    await add_skill(db, alice, "ops-guide", kind=SkillKind.GROUNDING, category="ops", tags=["x"])
    await add_skill(db, alice, "dev-report", kind=SkillKind.SKILL, category="dev", tags=["x"])

    assert await search_names(db, q="report", category="ops") == ["ops-report"]
    assert await search_names(db, q="ops", kind=SkillKind.GROUNDING, tags=["x"]) == ["ops-guide"]
    assert await search_names(db, q="report", kind=SkillKind.GROUNDING) == []


async def test_search_filters_by_owner(db, alice, bob):
    await add_skill(db, alice, "alice-one")
    await add_skill(db, alice, "alice-two")
    await add_skill(db, bob, "bob-one")

    assert await search_names(db, owner_id=alice.id) == ["alice-one", "alice-two"]
    assert await search_names(db, owner_id=bob.id) == ["bob-one"]
    assert await search_names(db, owner_id="nobody") == []


async def test_search_sorts_by_name_updated_created_and_relevance(db, alice):
    old = await add_skill(db, alice, "gamma", description="topic")
    mid = await add_skill(db, alice, "alpha", description="topic topic")
    new = await add_skill(db, alice, "beta", description="nothing")
    for skill, created, updated in (
        (old, datetime(2024, 1, 1), datetime(2024, 6, 1)),
        (mid, datetime(2024, 2, 1), datetime(2024, 3, 1)),
        (new, datetime(2024, 3, 1), datetime(2024, 9, 1)),
    ):
        skill.created_at = created
        skill.updated_at = updated
    await db.flush()

    assert await search_names(db, sort="name") == ["alpha", "beta", "gamma"]
    assert await search_names(db, sort="updated") == ["beta", "gamma", "alpha"]
    assert await search_names(db, sort="created") == ["beta", "alpha", "gamma"]
    assert await search_names(db, q="topic", sort="relevance") == ["alpha", "gamma"]
    assert await search_names(db, q="topic", sort="name") == ["alpha", "gamma"]
    assert await search_names(db, q="topic", sort="created") == ["alpha", "gamma"]
    assert await search_names(db, sort="relevance") == ["alpha", "beta", "gamma"]
    assert await search_names(db) == ["alpha", "beta", "gamma"]


async def test_search_default_sort_is_relevance_with_query_and_name_without(db, alice):
    await add_skill(db, alice, "aaa", description="mention")
    await add_skill(db, alice, "zzz", display_name="Mention", description="")
    await add_skill(db, alice, "mention", display_name="Other")

    assert await search_names(db, q="mention") == ["mention", "zzz", "aaa"]
    assert await search_names(db) == ["aaa", "mention", "zzz"]


async def test_search_rejects_unknown_sort(db, alice):
    await add_skill(db, alice, "anything")

    with pytest.raises(ValueError, match="sort"):
        await skill_repo.search_skills(db, SkillQuery(sort="popularity"))


async def test_search_paginates_and_reports_the_real_total(db, alice):
    for index in range(1, 8):
        await add_skill(db, alice, f"item-{index}")
    await add_skill(db, alice, "other-thing")

    first, total = await skill_repo.search_skills(db, SkillQuery(q="item", limit=3, offset=0))
    second, _ = await skill_repo.search_skills(db, SkillQuery(q="item", limit=3, offset=3))
    last, _ = await skill_repo.search_skills(db, SkillQuery(q="item", limit=3, offset=6))
    beyond, beyond_total = await skill_repo.search_skills(
        db, SkillQuery(q="item", limit=3, offset=30)
    )
    nothing, zero_total = await skill_repo.search_skills(db, SkillQuery(q="item", limit=0))

    assert total == 7
    assert [s.name for s in first] == ["item-1", "item-2", "item-3"]
    assert [s.name for s in second] == ["item-4", "item-5", "item-6"]
    assert [s.name for s in last] == ["item-7"]
    assert (beyond, beyond_total) == ([], 7)
    assert (nothing, zero_total) == ([], 7)


async def test_search_clamps_negative_limit_and_offset(db, alice):
    for index in range(3):
        await add_skill(db, alice, f"entry-{index}")

    items, total = await skill_repo.search_skills(db, SkillQuery(limit=-1, offset=-5))
    assert (items, total) == ([], 3)

    items, total = await skill_repo.search_skills(db, SkillQuery(limit=2, offset=-5))
    assert [s.name for s in items] == ["entry-0", "entry-1"]
    assert total == 3


async def test_search_binds_clamped_pagination_values(db, engine, alice):
    await add_skill(db, alice, "only")

    with record_statements(engine) as statements:
        await skill_repo.search_skills(db, SkillQuery(limit=2, offset=-5))

    paginated = [params for sql, params in statements if " LIMIT " in sql.upper()]
    assert len(paginated) == 1
    assert tuple(paginated[0][-2:]) == (2, 0)


async def test_search_defaults_to_twenty_results(db, alice):
    for index in range(25):
        await add_skill(db, alice, f"bulk-{index:02d}")

    items, total = await skill_repo.search_skills(db, SkillQuery())

    assert len(items) == 20
    assert total == 25


async def test_search_pages_in_sql_without_loading_every_row(db, engine, alice):
    for index in range(30):
        await add_skill(db, alice, f"bulk-{index:02d}", tags=["common", f"n{index}"])
    await db.commit()
    db.expunge_all()

    with record_statements(engine) as statements:
        items, total = await skill_repo.search_skills(db, SkillQuery(q="bulk", limit=5))

    assert total == 30
    assert len(items) == 5
    assert [item.tags[0] for item in items] == ["common"] * 5
    assert len(statements) == 3
    assert "count(" in statements[0][0].lower()
    assert "limit" in statements[1][0].lower()
    assert "order by skill_tags.tag" in statements[2][0].lower()
    loaded = [obj for obj in db.identity_map.values() if isinstance(obj, Skill)]
    assert len(loaded) == 5


async def test_like_comparisons_are_case_folded_and_escaped_for_portability(db, engine, alice):
    await add_skill(db, alice, "anything", tags=["x"])

    with record_statements(engine) as statements:
        await skill_repo.search_skills(db, SkillQuery(q="Foo bar", tags=["x"]))

    sql = " ".join(statement for statement, _ in statements)
    comparisons = re.findall(r"\bLIKE\b", sql)
    folded_and_escaped = re.findall(r"lower\([\w.]+\) LIKE \? ESCAPE '\\'", sql)
    assert comparisons
    assert len(comparisons) == len(folded_and_escaped)
    assert "ilike" not in sql.lower()


async def test_search_results_have_tags_loaded_without_lazy_io(db, alice):
    await add_skill(db, alice, "tagged", tags=["b", "a"])
    await db.commit()
    db.expunge_all()

    items, _ = await skill_repo.search_skills(db, SkillQuery())

    assert items[0].tags == ["a", "b"]


@pytest_asyncio.fixture
async def catalog(db, alice, bob, root):
    await add_skill(db, alice, "pub-alice", description="shared report")
    await add_skill(db, bob, "pub-bob", description="shared report")
    await add_skill(
        db,
        alice,
        "draft-alice",
        description="shared report",
        status=SkillStatus.DRAFT,
        latest_version=None,
    )
    await add_skill(
        db,
        bob,
        "draft-bob",
        description="shared report",
        status=SkillStatus.DRAFT,
        latest_version=None,
    )
    await add_skill(
        db,
        alice,
        "hidden-alice",
        description="shared report",
        status=SkillStatus.UNPUBLISHED,
    )
    await add_skill(
        db,
        alice,
        "orphan-alice",
        description="shared report",
        status=SkillStatus.PUBLISHED,
        latest_version=None,
    )


ALL_SIX = {"pub-alice", "pub-bob", "draft-alice", "draft-bob", "hidden-alice", "orphan-alice"}


async def test_visibility_of_listings_and_totals_per_viewer(db, alice, bob, root, catalog):
    cases = [
        ("anonymous", {}, {"pub-alice", "pub-bob"}),
        (
            "other publisher",
            {"viewer_id": bob.id},
            {"pub-alice", "pub-bob", "draft-bob"},
        ),
        (
            "owner",
            {"viewer_id": alice.id},
            {"pub-alice", "pub-bob", "draft-alice", "hidden-alice", "orphan-alice"},
        ),
        ("admin", {"viewer_id": root.id, "viewer_is_admin": True}, ALL_SIX),
        ("admin without id", {"viewer_is_admin": True}, ALL_SIX),
        ("unknown viewer id", {"viewer_id": "stranger"}, {"pub-alice", "pub-bob"}),
    ]

    for label, viewer, expected in cases:
        for q in (None, "shared", "REPORT"):
            items, total = await skill_repo.search_skills(db, SkillQuery(q=q, limit=100, **viewer))
            assert {item.name for item in items} == expected, (label, q)
            assert total == len(expected), (label, q)


async def test_total_never_counts_entries_the_viewer_cannot_see(db, alice, catalog):
    items, total = await skill_repo.search_skills(db, SkillQuery(limit=1))

    assert total == 2
    assert len(items) == 1
    stored = (await db.execute(text("SELECT count(*) FROM skills"))).scalar_one()
    assert stored == 6


async def test_published_entries_without_latest_version_stay_hidden_from_others(
    db, alice, bob, catalog
):
    assert "orphan-alice" not in await search_names(db)
    assert "orphan-alice" not in await search_names(db, viewer_id=bob.id)
    assert "orphan-alice" in await search_names(db, viewer_id=alice.id)


async def test_status_filter_never_widens_visibility(db, alice, bob, root, catalog):
    drafts = [SkillStatus.DRAFT]
    cases = [
        ("anonymous", {}, drafts, set()),
        ("other publisher", {"viewer_id": bob.id}, drafts, {"draft-bob"}),
        ("owner", {"viewer_id": alice.id}, drafts, {"draft-alice"}),
        (
            "admin",
            {"viewer_id": root.id, "viewer_is_admin": True},
            drafts,
            {"draft-alice", "draft-bob"},
        ),
        ("anonymous", {}, [SkillStatus.UNPUBLISHED], set()),
        ("other publisher", {"viewer_id": bob.id}, [SkillStatus.UNPUBLISHED], set()),
        ("owner", {"viewer_id": alice.id}, [SkillStatus.UNPUBLISHED], {"hidden-alice"}),
        (
            "owner",
            {"viewer_id": alice.id},
            [SkillStatus.PUBLISHED],
            {"pub-alice", "pub-bob", "orphan-alice"},
        ),
        (
            "owner",
            {"viewer_id": alice.id},
            [SkillStatus.DRAFT, SkillStatus.UNPUBLISHED],
            {"draft-alice", "hidden-alice"},
        ),
    ]

    for label, viewer, statuses, expected in cases:
        items, total = await skill_repo.search_skills(
            db, SkillQuery(statuses=statuses, limit=100, **viewer)
        )
        assert {item.name for item in items} == expected, (label, statuses)
        assert total == len(expected), (label, statuses)


async def test_an_empty_status_set_matches_nothing_while_none_means_no_filter(
    db, alice, root, catalog
):
    viewer = {"viewer_id": root.id, "viewer_is_admin": True, "limit": 100}

    assert await skill_repo.search_skills(db, SkillQuery(statuses=[], **viewer)) == ([], 0)
    assert await skill_repo.search_skills(db, SkillQuery(statuses=(), viewer_id=alice.id)) == (
        [],
        0,
    )
    everything, total = await skill_repo.search_skills(db, SkillQuery(statuses=None, **viewer))
    assert total == len(everything) == len(ALL_SIX)


async def test_owner_filter_never_widens_visibility(db, alice, bob, root, catalog):
    cases = [
        ("anonymous", {}, {"pub-alice"}),
        ("other publisher", {"viewer_id": bob.id}, {"pub-alice"}),
        (
            "owner",
            {"viewer_id": alice.id},
            {"pub-alice", "draft-alice", "hidden-alice", "orphan-alice"},
        ),
        (
            "admin",
            {"viewer_id": root.id, "viewer_is_admin": True},
            {"pub-alice", "draft-alice", "hidden-alice", "orphan-alice"},
        ),
    ]

    for label, viewer, expected in cases:
        items, total = await skill_repo.search_skills(
            db, SkillQuery(owner_id=alice.id, limit=100, **viewer)
        )
        assert {item.name for item in items} == expected, label
        assert total == len(expected), label


async def test_a_hidden_entry_matching_the_query_is_found_only_by_its_owner_and_admins(
    db, alice, bob, root
):
    await add_skill(
        db,
        alice,
        "secret-plan",
        description="codename",
        status=SkillStatus.DRAFT,
        latest_version=None,
        tags=["classified"],
        category="intel",
    )

    assert await search_names(db, q="codename") == []
    assert await search_names(db, q="codename", viewer_id=bob.id) == []
    assert await search_names(db, tags=["classified"], viewer_id=bob.id) == []
    assert await search_names(db, category="intel", viewer_id=bob.id) == []
    assert await search_names(db, q="codename", viewer_id=alice.id) == ["secret-plan"]
    assert await search_names(db, q="codename", viewer_id=root.id, viewer_is_admin=True) == [
        "secret-plan"
    ]


async def test_tag_counts_follow_visibility_ordering_kind_and_limit(db, alice, bob, root):
    await add_skill(db, alice, "a1", tags=["shared", "alpha"])
    await add_skill(db, bob, "b1", tags=["shared"], kind=SkillKind.GROUNDING)
    await add_skill(
        db,
        alice,
        "a-draft",
        tags=["shared", "secret"],
        status=SkillStatus.DRAFT,
        latest_version=None,
    )
    await add_skill(db, alice, "a2", tags=["zeta", "beta"])

    anonymous = await skill_repo.count_tags(db)
    other = await skill_repo.count_tags(db, viewer_id=bob.id)
    owner = await skill_repo.count_tags(db, viewer_id=alice.id)
    admin = await skill_repo.count_tags(db, viewer_id=root.id, viewer_is_admin=True)

    expected_public = [("shared", 2), ("alpha", 1), ("beta", 1), ("zeta", 1)]
    expected_owner = [("shared", 3), ("alpha", 1), ("beta", 1), ("secret", 1), ("zeta", 1)]
    assert anonymous == expected_public
    assert other == expected_public
    assert owner == expected_owner
    assert admin == expected_owner
    assert await skill_repo.count_tags(db, kind=SkillKind.GROUNDING) == [("shared", 1)]
    assert await skill_repo.count_tags(db, kind=SkillKind.SKILL) == [
        ("alpha", 1),
        ("beta", 1),
        ("shared", 1),
        ("zeta", 1),
    ]
    assert await skill_repo.count_tags(db, limit=2) == [("shared", 2), ("alpha", 1)]
    assert await skill_repo.count_tags(db, limit=0) == []


async def test_category_counts_follow_visibility_ordering_kind_and_limit(db, alice, bob, root):
    await add_skill(db, alice, "a1", category="ops")
    await add_skill(db, bob, "b1", category="ops", kind=SkillKind.GROUNDING)
    await add_skill(db, bob, "b2", category="dev")
    await add_skill(db, alice, "a-none")
    await add_skill(
        db, alice, "a-draft", category="private", status=SkillStatus.DRAFT, latest_version=None
    )

    assert await skill_repo.count_categories(db) == [("ops", 2), ("dev", 1)]
    assert await skill_repo.count_categories(db, viewer_id=bob.id) == [("ops", 2), ("dev", 1)]
    assert await skill_repo.count_categories(db, viewer_id=alice.id) == [
        ("ops", 2),
        ("dev", 1),
        ("private", 1),
    ]
    assert await skill_repo.count_categories(db, viewer_id=root.id, viewer_is_admin=True) == [
        ("ops", 2),
        ("dev", 1),
        ("private", 1),
    ]
    assert await skill_repo.count_categories(db, kind=SkillKind.GROUNDING) == [("ops", 1)]
    assert await skill_repo.count_categories(db, limit=1) == [("ops", 2)]
    assert await skill_repo.count_categories(db, limit=0) == []


async def test_counts_on_an_empty_registry(db):
    assert await skill_repo.count_tags(db) == []
    assert await skill_repo.count_categories(db) == []
