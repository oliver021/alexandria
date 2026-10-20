from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.auth.password import verify_password
from app.models import User, UserRole
from app.repositories import user_repo
from tests.test_repositories import create_test_engine

pytestmark = pytest.mark.usefixtures("fast_password_hashing")


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


async def add_user(db: AsyncSession, username: str = "alice", **overrides) -> User:
    values = {
        "username": username,
        "email": f"{username}@example.com",
        "password": "password123",
    }
    values.update(overrides)
    return await user_repo.create_user(db, **values)


async def count_users(db: AsyncSession) -> int:
    return (await db.execute(text("SELECT count(*) FROM users"))).scalar_one()


async def test_create_user_hashes_the_password_and_loads_server_values(db):
    user = await add_user(db, password="Zx9-unique-Passw0rd!")

    assert uuid.UUID(user.id).version == 4
    assert user.password_hash != "Zx9-unique-Passw0rd!"
    assert verify_password("Zx9-unique-Passw0rd!", user.password_hash)
    assert (user.role, user.is_active) == (UserRole.PUBLISHER, True)
    assert user.created_at is not None


async def test_create_user_stores_the_requested_role_as_its_lowercase_value(db):
    admin = await add_user(db, "root", role=UserRole.ADMIN)

    stored = (
        await db.execute(text("SELECT role FROM users WHERE id = :id"), {"id": admin.id})
    ).scalar_one()

    assert admin.role is UserRole.ADMIN
    assert stored == "admin"


async def test_usernames_and_emails_must_be_unique(db):
    await add_user(db, "alice")

    with pytest.raises(IntegrityError):
        await add_user(db, "alice", email="other@example.com")
    await db.rollback()
    await add_user(db, "alice")
    with pytest.raises(IntegrityError):
        await add_user(db, "bob", email="alice@example.com")


async def test_users_are_found_by_id_and_by_username(db):
    user = await add_user(db)
    db.expunge_all()

    by_id = await user_repo.get_user(db, user.id)
    by_name = await user_repo.get_user_by_username(db, "alice")

    assert by_id is by_name
    assert by_id.email == "alice@example.com"
    assert await user_repo.get_user(db, "missing") is None
    assert await user_repo.get_user_by_username(db, "ALICE") is None
    assert await user_repo.get_user_by_username(db, "missing") is None


async def test_list_users_returns_every_user_newest_first(db):
    first = await add_user(db, "first")
    await db.execute(
        text("UPDATE users SET created_at = '2020-01-01 00:00:00' WHERE id = :id"),
        {"id": first.id},
    )
    second = await add_user(db, "second")

    listed = await user_repo.list_users(db)

    assert [user.username for user in listed] == ["second", "first"]
    assert listed[0] is second


async def test_deactivate_user_flags_the_account_and_is_idempotent(db):
    user = await add_user(db)

    deactivated = await user_repo.deactivate_user(db, user)
    again = await user_repo.deactivate_user(db, user)

    assert deactivated is user and again is user
    assert user.is_active is False
    stored = (await db.execute(text("SELECT is_active FROM users"))).scalar_one()
    assert not stored


async def test_create_user_flushes_but_never_commits(db):
    await db.commit()
    user = await add_user(db, "ephemeral")
    assert await user_repo.get_user(db, user.id) is user
    assert await count_users(db) == 1

    await db.rollback()

    assert await count_users(db) == 0
    assert await user_repo.get_user_by_username(db, "ephemeral") is None


async def test_deactivate_user_flushes_but_never_commits(db):
    user = await add_user(db)
    user_id = user.id
    await db.commit()

    await user_repo.deactivate_user(db, user)
    assert (await db.execute(text("SELECT is_active FROM users"))).scalar_one() == 0
    await db.rollback()

    assert (await db.execute(text("SELECT is_active FROM users"))).scalar_one() == 1
    assert (await user_repo.get_user(db, user_id)).is_active is True


async def test_a_failure_after_create_user_leaves_no_user_behind(db):
    await db.commit()

    with pytest.raises(RuntimeError):
        await add_user(db, "doomed")
        await add_user(db, "survivor")
        raise RuntimeError("the use case failed after the repository calls")
    await db.rollback()

    assert await count_users(db) == 0


async def test_a_repository_write_is_durable_only_after_the_caller_commits(engine, db):
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await add_user(db, "rolled-back")
    await db.rollback()

    async with factory() as other:
        await add_user(other, "committed")
        await other.commit()

    async with factory() as check:
        assert [item.username for item in await user_repo.list_users(check)] == ["committed"]
