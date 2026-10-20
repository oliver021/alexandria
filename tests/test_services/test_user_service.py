from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.auth.local import LocalAuthProvider
from app.auth.password import MAX_PASSWORD_LENGTH
from app.models.schemas import UserCreate, UserResponse
from app.models.token import TokenScope
from app.models.user import User, UserRole
from app.repositories import user_repo
from app.services.auth_service import AuthService
from app.services.errors import (
    ConflictError,
    DomainError,
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
    ValidationError,
)
from app.services.principal import Principal
from app.services.user_service import UserService

pytestmark = pytest.mark.usefixtures("fast_password_hashing")

SECRET = "user-service-signing-secret"
RESPONSE_FIELDS = {"id", "username", "email", "role", "is_active", "created_at"}


def actor(role: UserRole, scope: TokenScope, name: str) -> Principal:
    return Principal(user_id=f"{name}-id", username=name, role=role, scope=scope)


ACTORS: dict[str, Principal | None] = {
    "anonymous": None,
    "publisher": actor(UserRole.PUBLISHER, TokenScope.WRITE, "pat"),
    "publisher_read": actor(UserRole.PUBLISHER, TokenScope.READ, "pat"),
    "admin_read": actor(UserRole.ADMIN, TokenScope.READ, "ada"),
    "admin": actor(UserRole.ADMIN, TokenScope.WRITE, "ada"),
}
DENIED_ACTORS = [name for name in ACTORS if name != "admin"]
EXPECTED_DENIAL: dict[str, type[DomainError]] = {
    "anonymous": UnauthorizedError,
    "publisher": ForbiddenError,
    "publisher_read": ForbiddenError,
    "admin_read": ForbiddenError,
}
ADMIN = ACTORS["admin"]


def new_user(
    username: str = "alice", *, role: str = "publisher", password: str = "password123"
) -> UserCreate:
    return UserCreate(
        username=username, email=f"{username}@example.com", password=password, role=role
    )


@pytest.fixture
def users(db_session: AsyncSession) -> UserService:
    return UserService(db_session)


@pytest.fixture
def auth(db_session: AsyncSession) -> AuthService:
    provider = LocalAuthProvider(secret=SECRET, algorithm="HS256", expiry_minutes=60)
    return AuthService(db_session, auth_provider=provider)


@pytest.fixture
async def victim(auth: AuthService) -> UserResponse:
    return await auth.create_user(new_user("victim"))


@contextmanager
def record_statements(engine: AsyncEngine) -> Iterator[list[str]]:
    statements: list[str] = []

    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)


async def count_users(db: AsyncSession) -> int:
    return (await db.execute(select(func.count()).select_from(User))).scalar_one()


Operation = Callable[[UserService, Principal | None, UserResponse], Any]

OPERATIONS: dict[str, Operation] = {
    "list": lambda users, who, target: users.list(who),
    "get": lambda users, who, target: users.get(who, target.id),
    "create": lambda users, who, target: users.create(who, new_user("newcomer")),
    "deactivate": lambda users, who, target: users.deactivate(who, target.id),
}


class TestAuthorizationMatrix:
    @pytest.mark.parametrize("operation", list(OPERATIONS))
    @pytest.mark.parametrize("name", list(ACTORS))
    async def test_only_an_admin_with_write_scope_may_manage_users(
        self, users, victim, operation, name
    ):
        call = OPERATIONS[operation]

        if name == "admin":
            assert await call(users, ACTORS[name], victim) is not None
            return
        with pytest.raises(DomainError) as raised:
            await call(users, ACTORS[name], victim)

        assert type(raised.value) is EXPECTED_DENIAL[name]

    @pytest.mark.parametrize("operation", list(OPERATIONS))
    @pytest.mark.parametrize("name", DENIED_ACTORS)
    async def test_a_denied_call_never_reaches_the_database(
        self, users, engine, db_session, victim, operation, name
    ):
        with record_statements(engine) as statements, pytest.raises(DomainError):
            await OPERATIONS[operation](users, ACTORS[name], victim)

        assert statements == []
        assert await count_users(db_session) == 1
        assert (await user_repo.get_user(db_session, victim.id)).is_active is True

    @pytest.mark.parametrize("operation", ["get", "deactivate"])
    @pytest.mark.parametrize("name", DENIED_ACTORS)
    @pytest.mark.parametrize(
        "user_id",
        [
            pytest.param(str(uuid.uuid4()), id="unknown-uuid"),
            pytest.param("", id="empty"),
            pytest.param("a\x00b", id="nul"),
            pytest.param("a\ud800b", id="lone-surrogate"),
        ],
    )
    async def test_callers_without_rights_learn_nothing_about_missing_ids(
        self, users, operation, name, user_id
    ):
        who = ACTORS[name]
        call = getattr(users, operation)

        with pytest.raises(DomainError) as raised:
            await call(who, user_id)

        assert type(raised.value) is EXPECTED_DENIAL[name]

    @pytest.mark.parametrize("name", DENIED_ACTORS)
    async def test_denied_creation_leaves_no_user_behind_even_with_hostile_data(
        self, users, db_session, name
    ):
        hostile = UserCreate.model_construct(
            username="ali\x00ce", email="a@example.com", password="x" * 5000, role="root"
        )

        with pytest.raises(DomainError) as raised:
            await users.create(ACTORS[name], hostile)

        assert type(raised.value) is EXPECTED_DENIAL[name]
        assert await count_users(db_session) == 0

    async def test_the_authorization_error_codes_match_the_contract(self, users):
        with pytest.raises(UnauthorizedError) as unauthorized:
            await users.list(None)
        with pytest.raises(ForbiddenError) as forbidden:
            await users.list(ACTORS["publisher"])

        assert (unauthorized.value.code, forbidden.value.code) == ("UNAUTHORIZED", "FORBIDDEN")


class TestList:
    async def test_an_admin_sees_every_account_newest_first_including_inactive_ones(
        self, users, auth
    ):
        first = await auth.create_user(new_user("first"))
        second = await auth.create_user(new_user("second", role="admin"))
        third = await auth.create_user(new_user("third"))
        await users.deactivate(ADMIN, second.id)

        listed = await users.list(ADMIN)

        assert {item.id for item in listed} == {first.id, second.id, third.id}
        assert {item.username: item.is_active for item in listed} == {
            "first": True,
            "second": False,
            "third": True,
        }
        stamps = [item.created_at for item in listed]
        assert stamps == sorted(stamps, reverse=True)

    async def test_an_empty_registry_lists_nothing(self, users):
        assert await users.list(ADMIN) == []


class TestGet:
    async def test_returns_the_stored_user(self, users, victim):
        fetched = await users.get(ADMIN, victim.id)

        assert fetched == victim
        assert fetched.username == "victim"

    @pytest.mark.parametrize(
        "user_id",
        [
            pytest.param(str(uuid.uuid4()), id="unknown-uuid"),
            pytest.param("", id="empty"),
            pytest.param("not-a-uuid", id="not-a-uuid"),
            pytest.param("' OR '1'='1", id="sql-fragment"),
            pytest.param("a\x00b", id="nul"),
            pytest.param("a\ud800b", id="lone-surrogate"),
        ],
    )
    async def test_an_unknown_user_is_not_found(self, users, user_id):
        with pytest.raises(NotFoundError, match="User not found") as raised:
            await users.get(ADMIN, user_id)

        assert raised.value.code == "NOT_FOUND"


class TestCreate:
    async def test_stores_the_user_with_the_requested_role_and_a_working_password(
        self, users, auth, db_session
    ):
        publisher = await users.create(ADMIN, new_user("pat", password="Zx9-unique-Passw0rd!"))
        admin = await users.create(ADMIN, new_user("ada", role="admin"))

        assert (publisher.role, admin.role) == ("publisher", "admin")
        assert publisher.is_active is True
        stored = await user_repo.get_user(db_session, publisher.id)
        assert stored.password_hash != "Zx9-unique-Passw0rd!"
        logged_in, _ = await auth.authenticate("pat", "Zx9-unique-Passw0rd!")
        assert logged_in.id == publisher.id

    async def test_a_duplicate_username_conflicts_and_the_session_stays_usable(
        self, users, db_session
    ):
        await users.create(ADMIN, new_user("dup"))

        with pytest.raises(ConflictError, match="already exists") as raised:
            await users.create(ADMIN, new_user("dup"))

        assert raised.value.code == "CONFLICT"
        assert await count_users(db_session) == 1
        assert (await users.create(ADMIN, new_user("other"))).username == "other"

    async def test_a_duplicate_email_conflicts_and_rolls_the_transaction_back(
        self, users, db_session
    ):
        await users.create(ADMIN, new_user("first"))
        clash = new_user("second").model_copy(update={"email": "first@example.com"})

        with pytest.raises(ConflictError):
            await users.create(ADMIN, clash)

        assert not db_session.in_transaction()
        assert await count_users(db_session) == 1
        assert (await users.create(ADMIN, new_user("third"))).username == "third"

    @pytest.mark.parametrize("role", ["root", "ADMIN", "", " admin", "publisher ", "superuser"])
    async def test_an_invalid_role_is_a_validation_error(self, users, db_session, role):
        with pytest.raises(ValidationError, match="admin, publisher"):
            await users.create(ADMIN, new_user(role=role))

        assert await count_users(db_session) == 0

    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param({"password": "pass\x00word123"}, id="nul-in-password"),
            pytest.param({"username": "ali\x00ce"}, id="nul-in-username"),
            pytest.param({"email": "ali\x00ce@example.com"}, id="nul-in-email"),
            pytest.param({"username": "ali\ud800ce"}, id="surrogate-in-username"),
            pytest.param({"password": "x" * (MAX_PASSWORD_LENGTH + 1)}, id="oversized-password"),
        ],
    )
    async def test_text_the_hasher_or_database_cannot_handle_is_rejected(
        self, users, db_session, overrides
    ):
        data = UserCreate.model_construct(**{**new_user().model_dump(), **overrides})

        with pytest.raises(ValidationError):
            await users.create(ADMIN, data)

        assert await count_users(db_session) == 0

    async def test_unexpected_failures_roll_everything_back(self, users, db_session, monkeypatch):
        async def flush_then_fail(db, *, username, email, password, role):
            db.add(User(username=username, email=email, password_hash="unused", role=role))
            await db.flush()
            raise RuntimeError("storage went away")

        with monkeypatch.context() as patch:
            patch.setattr(user_repo, "create_user", flush_then_fail)
            with pytest.raises(RuntimeError):
                await users.create(ADMIN, new_user())

        assert not db_session.in_transaction()
        assert await count_users(db_session) == 0
        assert (await users.create(ADMIN, new_user())).username == "alice"

    async def test_the_internal_path_and_the_admin_path_create_the_same_account(self, users, auth):
        internal = await auth.create_user(new_user("internal"))
        through_admin = await users.create(ADMIN, new_user("external"))

        assert type(internal) is type(through_admin) is UserResponse
        assert (internal.role, internal.is_active) == (through_admin.role, through_admin.is_active)
        assert set(internal.model_dump()) == set(through_admin.model_dump()) == RESPONSE_FIELDS

    @pytest.mark.parametrize(
        "data",
        [
            pytest.param(new_user(role="root"), id="invalid-role"),
            pytest.param(
                UserCreate.model_construct(
                    username="a", email="a@example.com", password="p\x00q", role="publisher"
                ),
                id="nul",
            ),
            pytest.param(
                UserCreate.model_construct(
                    username="a",
                    email="a@example.com",
                    password="p" * (MAX_PASSWORD_LENGTH + 1),
                    role="publisher",
                ),
                id="oversized",
            ),
        ],
    )
    async def test_both_creation_paths_reject_the_same_inputs_the_same_way(self, users, auth, data):
        with pytest.raises(ValidationError) as internal:
            await auth.create_user(data)
        with pytest.raises(ValidationError) as through_admin:
            await users.create(ADMIN, data)

        assert str(internal.value) == str(through_admin.value)


class TestDeactivate:
    async def test_flags_the_account_and_returns_the_snapshot(self, users, victim):
        deactivated = await users.deactivate(ADMIN, victim.id)

        assert victim.is_active is True
        assert deactivated.is_active is False
        assert deactivated.id == victim.id
        assert (await users.get(ADMIN, victim.id)).is_active is False

    async def test_blocks_the_login_of_the_account(self, users, auth, victim):
        await auth.authenticate("victim", "password123")

        await users.deactivate(ADMIN, victim.id)

        with pytest.raises(UnauthorizedError):
            await auth.authenticate("victim", "password123")

    async def test_is_idempotent(self, users, victim):
        await users.deactivate(ADMIN, victim.id)

        again = await users.deactivate(ADMIN, victim.id)

        assert again.is_active is False

    async def test_an_unknown_user_is_not_found(self, users):
        with pytest.raises(NotFoundError, match="User not found"):
            await users.deactivate(ADMIN, str(uuid.uuid4()))

    async def test_an_admin_cannot_deactivate_their_own_account(self, users, auth):
        boss = await auth.create_user(new_user("boss", role="admin"))
        principal = Principal(
            user_id=boss.id, username="boss", role=UserRole.ADMIN, scope=TokenScope.WRITE
        )

        with pytest.raises(ConflictError, match="own account"):
            await users.deactivate(principal, boss.id)

        assert (await users.get(principal, boss.id)).is_active is True

    async def test_an_admin_may_deactivate_another_admin(self, users, auth):
        boss = await auth.create_user(new_user("boss", role="admin"))
        peer = await auth.create_user(new_user("peer", role="admin"))
        principal = Principal(
            user_id=boss.id, username="boss", role=UserRole.ADMIN, scope=TokenScope.WRITE
        )

        assert (await users.deactivate(principal, peer.id)).is_active is False

    async def test_unexpected_failures_roll_the_flag_back(
        self, users, db_session, victim, monkeypatch
    ):
        async def flush_then_fail(db, target):
            target.is_active = False
            await db.flush()
            raise RuntimeError("storage went away")

        with monkeypatch.context() as patch:
            patch.setattr(user_repo, "deactivate_user", flush_then_fail)
            with pytest.raises(RuntimeError):
                await users.deactivate(ADMIN, victim.id)

        assert not db_session.in_transaction()
        assert (await users.get(ADMIN, victim.id)).is_active is True


class TestResponses:
    async def test_every_operation_returns_response_dtos_never_orm_rows(self, users, auth):
        created = await users.create(ADMIN, new_user("alice"))
        await users.create(ADMIN, new_user("bob"))
        fetched = await users.get(ADMIN, created.id)
        listed = await users.list(ADMIN)
        deactivated = await users.deactivate(ADMIN, created.id)

        returned = [created, fetched, deactivated, *listed]

        assert len(listed) == 2
        assert all(type(item) is UserResponse for item in returned)
        assert not any(isinstance(item, User) for item in returned)
        for item in returned:
            assert set(item.model_dump()) == RESPONSE_FIELDS
            assert not hasattr(item, "password_hash")
            assert "$2" not in item.model_dump_json()

    async def test_responses_stay_readable_when_the_session_expires_objects_on_commit(self, engine):
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=True)
        async with factory() as session:
            service = UserService(session)
            created = await service.create(ADMIN, new_user())
            deactivated = await service.deactivate(ADMIN, created.id)
            fetched = await service.get(ADMIN, created.id)
            listed = await service.list(ADMIN)

        assert (created.username, created.email, created.is_active) == (
            "alice",
            "alice@example.com",
            True,
        )
        assert (deactivated.is_active, fetched.is_active, listed[0].is_active) == (False,) * 3


class TestAuditTrail:
    async def test_creation_and_deactivation_record_who_acted(self, users, caplog):
        caplog.set_level(logging.INFO, logger="app.services.user")

        created = await users.create(ADMIN, new_user("traced", password="Zx9-unique-Passw0rd!"))
        await users.deactivate(ADMIN, created.id)

        records = {r.getMessage(): r for r in caplog.records if r.name == "app.services.user"}
        creation = records["User created: traced"].extra_data
        deactivation = records["User deactivated: traced"].extra_data
        assert creation["created_by"] == ADMIN.user_id
        assert creation["user_id"] == created.id
        assert deactivation["deactivated_by"] == ADMIN.user_id
        assert "Zx9-unique-Passw0rd!" not in str([r.__dict__ for r in caplog.records])


class TestAuthServiceNoLongerManagesUsers:
    @pytest.mark.parametrize("name", ["get_user", "list_users", "deactivate_user", "_require_user"])
    def test_the_account_management_methods_are_gone(self, name):
        assert not hasattr(AuthService, name)

    def test_the_internal_creation_path_remains(self):
        assert callable(AuthService.create_user)
