from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.exc import TimeoutError as PoolTimeout
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import QueuePool, StaticPool

from app.config.settings import DatabaseSettings, settings
from app.db import session as session_module
from app.db.session import (
    configure_sqlite_engine,
    enable_sqlite_foreign_keys,
    enable_sqlite_unicode_lower,
    pool_options,
)
from app.models import Base, Skill, SkillKind, SkillStatus

MISSING_USER = "00000000-0000-4000-8000-000000000000"


def orphan_skill() -> Skill:
    return Skill(
        name="orphan",
        display_name="Orphan",
        description="Owned by nobody",
        kind=SkillKind.SKILL,
        owner_id=MISSING_USER,
        status=SkillStatus.DRAFT,
    )


LIKE_OVER_LOWER = "SELECT lower(:value) LIKE :pattern ESCAPE '\\'"

UNICODE_CASES = [
    ("ÁRBOL", "árbol"),
    ("Árbol de Decisión", "árbol de decisión"),
    ("ÉCOLE", "école"),
    ("ÑANDÚ", "ñandú"),
    ("Índice ÚTIL Über", "índice útil über"),
    ("ПРИВЕТ Мир", "привет мир"),
    ("ΑΘΗΝΑ", "αθηνα"),
    ("ŁÓDŹ", "łódź"),
    ("MiXeD CaSe 123", "mixed case 123"),
    ("", ""),
]


async def foreign_keys_pragma(engine) -> int:
    async with engine.connect() as connection:
        return (await connection.execute(text("PRAGMA foreign_keys"))).scalar_one()


async def sql_value(engine, statement: str, **parameters):
    async with engine.connect() as connection:
        return (await connection.execute(text(statement), parameters)).scalar_one()


async def test_the_fixture_engine_enforces_foreign_keys(engine, db_session):
    assert await foreign_keys_pragma(engine) == 1
    db_session.add(orphan_skill())
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_an_engine_without_the_listener_does_not_enforce_them():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        assert await foreign_keys_pragma(engine) == 0
        async with factory() as db:
            db.add(orphan_skill())
            await db.flush()
    finally:
        await engine.dispose()


async def test_enabling_foreign_keys_turns_the_pragma_on_for_new_connections():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    enable_sqlite_foreign_keys(engine)
    try:
        assert await foreign_keys_pragma(engine) == 1
    finally:
        await engine.dispose()


async def test_enabling_foreign_keys_twice_registers_one_listener():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    enable_sqlite_foreign_keys(engine)
    enable_sqlite_foreign_keys(engine)
    try:
        listeners = engine.sync_engine.pool.dispatch.connect
        assert sum(item is session_module._set_sqlite_foreign_keys for item in listeners) == 1
        assert await foreign_keys_pragma(engine) == 1
    finally:
        await engine.dispose()


def test_the_listener_also_applies_to_synchronous_sqlite_engines():
    engine = create_engine("sqlite://")
    enable_sqlite_foreign_keys(engine)
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
    engine.dispose()


def test_other_dialects_are_left_alone():
    stub = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    enable_sqlite_foreign_keys(stub)


def test_the_application_engine_enforces_foreign_keys():
    assert event.contains(
        session_module.engine.sync_engine, "connect", session_module._set_sqlite_foreign_keys
    )


def test_the_application_engine_registers_the_unicode_lower():
    assert event.contains(
        session_module.engine.sync_engine, "connect", session_module._register_unicode_lower
    )


@pytest.mark.parametrize(("source", "expected"), UNICODE_CASES)
async def test_the_fixture_engine_folds_the_case_of_non_ascii_letters(engine, source, expected):
    assert await sql_value(engine, "SELECT lower(:value)", value=source) == expected


@pytest.mark.parametrize(
    ("value", "pattern"),
    [
        ("Árbol de decisión", "%árbol%"),
        ("Árbol de decisión", "%árbol de%"),
        ("ARBOL DE DECISIÓN", "%decisión"),
        ("Para ÉCOLE normale", "%école%"),
        ("Ñandú ÁRBOL MÉTODO", "%ñandú%"),
        ("Ñandú ÁRBOL MÉTODO", "%método"),
        ("Ünïcödé ТЕКСТ", "%текст%"),
    ],
)
async def test_like_over_lower_matches_non_ascii_capitals(engine, value, pattern):
    assert await sql_value(engine, LIKE_OVER_LOWER, value=value, pattern=pattern) == 1


async def test_like_over_lower_still_rejects_text_that_does_not_contain_the_pattern(engine):
    assert await sql_value(engine, LIKE_OVER_LOWER, value="Árbol", pattern="%école%") == 0
    assert await sql_value(engine, LIKE_OVER_LOWER, value="Árbol", pattern="%arbol%") == 0


async def test_the_builtin_sqlite_lower_folds_only_ascii_letters():
    plain = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    try:
        if await sql_value(plain, "SELECT lower('Á')") == "á":
            pytest.skip("this SQLite build already folds the case of non-ASCII letters")
        assert await sql_value(plain, "SELECT lower(:value)", value="ÁRBOL Ñandú ABC") == (
            "Árbol Ñandú abc"
        )
        assert await sql_value(plain, LIKE_OVER_LOWER, value="Árbol", pattern="%árbol%") == 0
    finally:
        await plain.dispose()


async def test_the_unicode_lower_follows_the_builtin_contract_for_non_text_values(engine):
    assert await sql_value(engine, "SELECT lower(NULL)") is None
    assert await sql_value(engine, "SELECT lower(42)") == "42"
    assert await sql_value(engine, "SELECT lower(1.5)") == "1.5"
    assert await sql_value(engine, "SELECT lower(x'C38152')") == "ár"
    assert await sql_value(engine, "SELECT lower(x'')") == ""


async def test_the_unicode_lower_applies_to_columns_and_leaves_stored_text_untouched(
    engine, db_session
):
    await db_session.execute(text("CREATE TABLE notes (body TEXT)"))
    await db_session.execute(
        text("INSERT INTO notes (body) VALUES ('Árbol'), ('ÉCOLE'), (NULL), ('plain')")
    )
    folded = await db_session.execute(text("SELECT lower(body) FROM notes ORDER BY rowid"))
    stored = await db_session.execute(text("SELECT body FROM notes ORDER BY rowid"))
    hits = await db_session.execute(
        text("SELECT body FROM notes WHERE lower(body) LIKE '%rbol%' ORDER BY rowid")
    )

    assert [row[0] for row in folded] == ["árbol", "école", None, "plain"]
    assert [row[0] for row in stored] == ["Árbol", "ÉCOLE", None, "plain"]
    assert [row[0] for row in hits] == ["Árbol"]
    await db_session.rollback()


async def test_enabling_the_unicode_lower_turns_it_on_for_new_connections():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    enable_sqlite_unicode_lower(engine)
    try:
        assert await sql_value(engine, "SELECT lower('ÁRBOL')") == "árbol"
        assert await foreign_keys_pragma(engine) == 0
    finally:
        await engine.dispose()


async def test_enabling_the_unicode_lower_twice_registers_one_listener():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    enable_sqlite_unicode_lower(engine)
    enable_sqlite_unicode_lower(engine)
    try:
        listeners = engine.sync_engine.pool.dispatch.connect
        assert sum(item is session_module._register_unicode_lower for item in listeners) == 1
        assert await sql_value(engine, "SELECT lower('ÑANDÚ')") == "ñandú"
    finally:
        await engine.dispose()


async def test_configuring_an_engine_enables_foreign_keys_and_the_unicode_lower():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    configure_sqlite_engine(engine)
    configure_sqlite_engine(engine)
    try:
        listeners = engine.sync_engine.pool.dispatch.connect
        assert sum(item is session_module._set_sqlite_foreign_keys for item in listeners) == 1
        assert sum(item is session_module._register_unicode_lower for item in listeners) == 1
        assert await foreign_keys_pragma(engine) == 1
        assert await sql_value(engine, "SELECT lower('ÉCOLE')") == "école"
    finally:
        await engine.dispose()


async def test_every_pooled_connection_and_every_reconnection_folds_unicode_case(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pool.db'}")
    configure_sqlite_engine(engine)
    opened: list[object] = []
    event.listen(
        engine.sync_engine, "connect", lambda connection, record: opened.append(connection)
    )
    try:
        async with engine.connect() as first, engine.connect() as second:
            for connection in (first, second):
                folded = await connection.execute(text("SELECT lower('ÁÉÍÓÚ')"))
                assert folded.scalar_one() == "áéíóú"
        assert len(opened) == 2
        await engine.dispose()
        assert await sql_value(engine, "SELECT lower('ÑÜ')") == "ñü"
        assert len(opened) == 3
    finally:
        await engine.dispose()


def test_the_unicode_lower_also_applies_to_synchronous_sqlite_engines():
    engine = create_engine("sqlite://")
    enable_sqlite_unicode_lower(engine)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT lower('ÁRBOL')")).scalar_one() == "árbol"
    engine.dispose()


def test_other_dialects_are_left_alone_by_the_unicode_lower_and_the_full_configuration():
    stub = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    enable_sqlite_unicode_lower(stub)
    configure_sqlite_engine(stub)


@pytest.mark.parametrize(
    "url",
    [
        "sqlite+aiosqlite:///data/registry.db",
        "postgresql+asyncpg://user:secret@db.internal:5432/alexandria",
    ],
)
def test_pooled_databases_get_the_configured_pool_limits(url):
    database = DatabaseSettings(pool_size=3, max_overflow=2, pool_timeout=7.5)

    assert pool_options(url, database) == {
        "pool_size": 3,
        "max_overflow": 2,
        "pool_timeout": 7.5,
    }


@pytest.mark.parametrize("url", ["sqlite+aiosqlite:///:memory:", "sqlite+aiosqlite://"])
def test_databases_without_a_queue_pool_are_not_given_pool_limits(url):
    assert pool_options(url, DatabaseSettings(pool_size=3)) == {}


def test_the_application_engine_gets_its_pool_from_the_configuration():
    pool = session_module.engine.sync_engine.pool
    options = pool_options(session_module.database_url, settings.database)

    if not options:
        assert not isinstance(pool, QueuePool)
        return
    assert isinstance(pool, QueuePool)
    assert (pool.size(), pool._max_overflow, pool.timeout()) == (
        options["pool_size"],
        options["max_overflow"],
        options["pool_timeout"],
    )


async def test_a_configured_pool_refuses_connections_past_its_limit_after_the_timeout(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'limit.db'}"
    database = DatabaseSettings(pool_size=1, max_overflow=1, pool_timeout=0.2)
    engine = create_async_engine(url, **pool_options(url, database))
    try:
        async with engine.connect(), engine.connect():
            started = time.perf_counter()
            with pytest.raises(PoolTimeout):
                async with engine.connect():
                    pass
            waited = time.perf_counter() - started
            assert engine.pool.checkedout() == 2
        assert 0.15 <= waited < 5
        assert engine.pool.checkedout() == 0
        async with engine.connect() as connection:
            assert (await connection.execute(text("SELECT 1"))).scalar_one() == 1
    finally:
        await engine.dispose()


async def test_the_dependency_hands_out_a_session_and_closes_it_afterwards():
    generator = session_module.get_db()

    session = await anext(generator)

    assert isinstance(session, AsyncSession)
    assert not session.in_transaction()
    await generator.aclose()
    assert not session.in_transaction()
