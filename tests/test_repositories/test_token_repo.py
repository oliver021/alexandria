from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.models import ApiToken, TokenScope, User
from app.repositories import token_repo
from tests.test_repositories import create_test_engine, make_user

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def stored(moment: datetime) -> datetime:
    return moment.replace(tzinfo=None)


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


_counter = {"value": 0}


async def add_token(
    db: AsyncSession,
    user: User,
    *,
    name: str = "ci",
    scope: TokenScope = TokenScope.WRITE,
    expires_at: datetime | None = None,
    token_hash: str | None = None,
) -> ApiToken:
    _counter["value"] += 1
    digest = token_hash or f"{_counter['value']:064x}"
    return await token_repo.create_token(
        db,
        user_id=user.id,
        name=name,
        prefix=f"alx_{_counter['value']:08d}",
        token_hash=digest,
        scope=scope,
        expires_at=expires_at,
    )


async def test_create_token_persists_fields_and_loads_server_values(db, alice):
    expires = NOW + timedelta(days=30)

    token = await token_repo.create_token(
        db,
        user_id=alice.id,
        name="deploy bot",
        prefix="alx_abcdefgh",
        token_hash="f" * 64,
        scope=TokenScope.READ,
        expires_at=expires,
    )

    assert len(token.id) == 36
    assert token.user_id == alice.id
    assert token.name == "deploy bot"
    assert token.prefix == "alx_abcdefgh"
    assert token.token_hash == "f" * 64
    assert token.scope is TokenScope.READ
    assert token.expires_at == stored(expires)
    assert token.created_at is not None
    assert token.last_used_at is None
    assert token.revoked_at is None


async def test_scope_is_stored_as_its_lowercase_value(db, alice):
    await add_token(db, alice, scope=TokenScope.READ)
    await add_token(db, alice, scope=TokenScope.WRITE)

    raw = (await db.execute(text("SELECT scope FROM api_tokens ORDER BY scope"))).scalars().all()

    assert raw == ["read", "write"]


async def test_scope_accepts_plain_values_and_rejects_unknown_ones(db, alice):
    token = await add_token(db, alice, scope="read")

    assert token.scope is TokenScope.READ
    with pytest.raises(ValueError):
        await add_token(db, alice, scope="admin")
    assert (await db.execute(text("SELECT count(*) FROM api_tokens"))).scalar_one() == 1


async def test_scope_defaults_to_the_least_privileged_value(db, alice):
    token = ApiToken(user_id=alice.id, name="n", prefix="alx_12345678", token_hash="e" * 64)
    db.add(token)
    await db.flush()

    assert token.scope is TokenScope.READ


async def test_token_hash_must_be_unique(db, alice, bob):
    await add_token(db, alice, token_hash="a" * 64)

    with pytest.raises(IntegrityError):
        await add_token(db, bob, token_hash="a" * 64)


async def test_token_must_belong_to_an_existing_user(db):
    ghost = User(id="ghost-id", username="ghost", email="g@x.io", password_hash="x")

    with pytest.raises(IntegrityError):
        await add_token(db, ghost)


async def test_get_token_by_id_and_by_hash(db, alice):
    created = await add_token(db, alice, token_hash="b" * 64)
    db.expunge_all()

    by_id = await token_repo.get_token(db, created.id)
    by_hash = await token_repo.get_token_by_hash(db, "b" * 64)

    assert by_id is by_hash
    assert by_id.id == created.id
    assert await token_repo.get_token(db, "missing") is None
    assert await token_repo.get_token_by_hash(db, "c" * 64) is None
    assert await token_repo.get_token_by_hash(db, "B" * 64) is None
    assert await token_repo.get_token_by_hash(db, "") is None


async def test_get_active_token_by_hash_rejects_revoked_expired_and_unknown_tokens(db, alice):
    valid = await add_token(db, alice, token_hash="1" * 64)
    forever = await add_token(db, alice, token_hash="2" * 64, expires_at=NOW + timedelta(days=1))
    revoked = await add_token(db, alice, token_hash="3" * 64)
    await token_repo.revoke_token(db, revoked, now=NOW - timedelta(minutes=1))
    await add_token(db, alice, token_hash="4" * 64, expires_at=NOW - timedelta(seconds=1))
    await add_token(db, alice, token_hash="5" * 64, expires_at=NOW)

    assert await token_repo.get_active_token_by_hash(db, "1" * 64, now=NOW) is valid
    assert await token_repo.get_active_token_by_hash(db, "2" * 64, now=NOW) is forever
    assert await token_repo.get_active_token_by_hash(db, "3" * 64, now=NOW) is None
    assert await token_repo.get_active_token_by_hash(db, "4" * 64, now=NOW) is None
    assert await token_repo.get_active_token_by_hash(db, "5" * 64, now=NOW) is None
    assert await token_repo.get_active_token_by_hash(db, "6" * 64, now=NOW) is None
    assert await token_repo.get_active_token_by_hash(db, "", now=NOW) is None


async def test_get_active_token_by_hash_follows_the_clock(db, alice):
    token = await add_token(db, alice, token_hash="7" * 64, expires_at=NOW + timedelta(hours=1))

    assert await token_repo.get_active_token_by_hash(db, "7" * 64, now=NOW) is token
    later = NOW + timedelta(hours=1, seconds=1)
    assert await token_repo.get_active_token_by_hash(db, "7" * 64, now=later) is None


async def test_get_active_token_by_hash_defaults_to_the_current_time(db, alice):
    await add_token(
        db, alice, token_hash="8" * 64, expires_at=datetime.now(UTC) + timedelta(hours=1)
    )
    await add_token(
        db, alice, token_hash="9" * 64, expires_at=datetime.now(UTC) - timedelta(hours=1)
    )

    assert await token_repo.get_active_token_by_hash(db, "8" * 64) is not None
    assert await token_repo.get_active_token_by_hash(db, "9" * 64) is None


async def test_list_tokens_is_scoped_to_the_user_and_newest_first(db, alice, bob):
    oldest = await add_token(db, alice, name="oldest")
    newest = await add_token(db, alice, name="newest")
    middle = await add_token(db, alice, name="middle")
    foreign = await add_token(db, bob, name="foreign")
    for token, created in (
        (oldest, datetime(2025, 1, 1)),
        (middle, datetime(2025, 6, 1)),
        (newest, datetime(2025, 12, 1)),
        (foreign, datetime(2026, 1, 1)),
    ):
        token.created_at = created
    await db.flush()

    listed = await token_repo.list_tokens(db, alice.id)

    assert [t.name for t in listed] == ["newest", "middle", "oldest"]
    assert [t.name for t in await token_repo.list_tokens(db, bob.id)] == ["foreign"]
    assert await token_repo.list_tokens(db, "nobody") == []


async def test_list_tokens_includes_revoked_and_expired_unless_active_only(db, alice):
    live = await add_token(db, alice, name="live")
    forever = await add_token(db, alice, name="forever", expires_at=NOW + timedelta(days=1))
    revoked = await add_token(db, alice, name="revoked")
    await token_repo.revoke_token(db, revoked, now=NOW)
    await add_token(db, alice, name="expired", expires_at=NOW - timedelta(seconds=1))

    everything = await token_repo.list_tokens(db, alice.id, now=NOW)
    active = await token_repo.list_tokens(db, alice.id, active_only=True, now=NOW)

    assert {t.name for t in everything} == {"live", "forever", "revoked", "expired"}
    assert {t.name for t in active} == {live.name, forever.name}


async def test_count_active_tokens_ignores_revoked_expired_and_foreign_tokens(db, alice, bob):
    await add_token(db, alice)
    await add_token(db, alice, expires_at=NOW + timedelta(hours=1))
    revoked = await add_token(db, alice)
    await token_repo.revoke_token(db, revoked, now=NOW - timedelta(days=1))
    await add_token(db, alice, expires_at=NOW - timedelta(hours=1))
    await add_token(db, bob)
    await add_token(db, bob)

    assert await token_repo.count_active_tokens(db, alice.id, now=NOW) == 2
    assert await token_repo.count_active_tokens(db, bob.id, now=NOW) == 2
    assert await token_repo.count_active_tokens(db, "nobody", now=NOW) == 0


async def test_a_token_expiring_exactly_now_is_no_longer_active(db, alice):
    await add_token(db, alice, expires_at=NOW)
    await add_token(db, alice, expires_at=NOW + timedelta(microseconds=1))

    assert await token_repo.count_active_tokens(db, alice.id, now=NOW) == 1
    assert await token_repo.count_active_tokens(db, alice.id, now=NOW + timedelta(seconds=1)) == 0


async def test_active_counting_defaults_to_the_current_time(db, alice):
    await add_token(db, alice, expires_at=datetime.now(UTC) + timedelta(hours=1))
    await add_token(db, alice, expires_at=datetime.now(UTC) - timedelta(hours=1))

    assert await token_repo.count_active_tokens(db, alice.id) == 1
    assert len(await token_repo.list_tokens(db, alice.id, active_only=True)) == 1


async def test_revoke_token_marks_it_and_is_idempotent(db, alice):
    token = await add_token(db, alice)
    later = NOW + timedelta(hours=2)

    first = await token_repo.revoke_token(db, token, now=NOW)
    second = await token_repo.revoke_token(db, token, now=later)

    assert first is token
    assert second is token
    assert token.revoked_at == stored(NOW)
    assert await token_repo.count_active_tokens(db, alice.id, now=NOW) == 0
    raw = (await db.execute(text("SELECT revoked_at FROM api_tokens"))).scalar_one()
    assert raw is not None


async def test_revoke_token_defaults_to_the_current_time(db, alice):
    token = await add_token(db, alice)

    await token_repo.revoke_token(db, token)

    assert token.revoked_at is not None
    assert abs(token.revoked_at - stored(datetime.now(UTC))) < timedelta(minutes=1)


async def test_revoked_tokens_remain_findable_by_hash_for_the_service_to_reject(db, alice):
    token = await add_token(db, alice, token_hash="d" * 64)
    await token_repo.revoke_token(db, token, now=NOW)

    found = await token_repo.get_token_by_hash(db, "d" * 64)

    assert found is token
    assert found.revoked_at is not None


async def test_touch_last_used_without_interval_always_updates(db, alice):
    token = await add_token(db, alice)

    assert await token_repo.touch_last_used(db, token, now=NOW) is True
    assert token.last_used_at == stored(NOW)
    assert await token_repo.touch_last_used(db, token, now=NOW + timedelta(seconds=1)) is True
    assert token.last_used_at == stored(NOW + timedelta(seconds=1))
    raw = (await db.execute(text("SELECT last_used_at FROM api_tokens"))).scalar_one()
    assert raw is not None


async def test_touch_last_used_is_throttled_by_the_minimum_interval(db, alice):
    token = await add_token(db, alice)
    interval = timedelta(minutes=10)

    assert await token_repo.touch_last_used(db, token, now=NOW, min_interval=interval) is True
    assert token.last_used_at == stored(NOW)

    soon = NOW + timedelta(minutes=9, seconds=59)
    assert await token_repo.touch_last_used(db, token, now=soon, min_interval=interval) is False
    assert token.last_used_at == stored(NOW)

    exactly = NOW + interval
    assert await token_repo.touch_last_used(db, token, now=exactly, min_interval=interval) is True
    assert token.last_used_at == stored(exactly)

    assert (
        await token_repo.touch_last_used(
            db, token, now=exactly + timedelta(minutes=11), min_interval=interval
        )
        is True
    )
    assert token.last_used_at == stored(exactly + timedelta(minutes=11))


async def test_throttled_touch_leaves_the_database_row_untouched(db, alice):
    token = await add_token(db, alice)
    await token_repo.touch_last_used(db, token, now=NOW, min_interval=timedelta(minutes=10))

    await token_repo.touch_last_used(
        db, token, now=NOW + timedelta(minutes=1), min_interval=timedelta(minutes=10)
    )

    raw = (await db.execute(text("SELECT last_used_at FROM api_tokens"))).scalar_one()
    assert raw.startswith("2026-01-01 12:00:00")


async def test_touch_last_used_only_affects_the_given_token(db, alice, bob):
    target = await add_token(db, alice)
    bystander = await add_token(db, alice)
    foreign = await add_token(db, bob)

    await token_repo.touch_last_used(db, target, now=NOW)
    await token_repo.touch_last_used(db, target, now=NOW, min_interval=timedelta(minutes=10))

    rows = (await db.execute(text("SELECT id, last_used_at FROM api_tokens ORDER BY id"))).all()
    touched = {row.id for row in rows if row.last_used_at is not None}
    assert touched == {target.id}
    await db.refresh(bystander)
    await db.refresh(foreign)
    assert bystander.last_used_at is None
    assert foreign.last_used_at is None


async def test_touch_last_used_reports_missing_tokens(db, alice):
    ghost = ApiToken(id="not-persisted")

    assert await token_repo.touch_last_used(db, ghost, now=NOW) is False
    assert await token_repo.touch_last_used(db, ghost, now=NOW, min_interval=timedelta(1)) is False


async def test_token_repository_writes_flush_but_never_commit(db, alice, bob):
    await db.commit()
    token = await add_token(db, alice, token_hash="9" * 64)
    await token_repo.touch_last_used(db, token, now=NOW)
    await token_repo.revoke_token(db, await add_token(db, bob), now=NOW)
    assert await token_repo.get_token_by_hash(db, "9" * 64) is not None

    await db.rollback()

    assert await token_repo.get_token_by_hash(db, "9" * 64) is None
    assert (await db.execute(text("SELECT count(*) FROM api_tokens"))).scalar_one() == 0
