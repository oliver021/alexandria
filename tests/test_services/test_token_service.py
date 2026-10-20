from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import types
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config.settings import settings
from app.db.base import Base
from app.logging import JSONFormatter
from app.models.schemas import TokenCreate, TokenCreated, TokenResponse
from app.models.token import ApiToken, TokenScope
from app.models.user import UserRole
from app.repositories import token_repo, user_repo
from app.services import token_service as token_service_module
from app.services.errors import (
    ConflictError,
    DomainError,
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
    ValidationError,
)
from app.services.principal import Principal
from app.services.token_service import LAST_USED_INTERVAL, TokenService, hash_token

pytestmark = pytest.mark.usefixtures("fast_password_hashing")

T0 = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
TOKEN_FORMAT = re.compile(r"alx_[A-Za-z0-9_-]{43}")
RESPONSE_FIELDS = {
    "id",
    "name",
    "prefix",
    "scope",
    "created_at",
    "expires_at",
    "last_used_at",
    "revoked_at",
}


class FakeClock:
    def __init__(self, start: datetime = T0) -> None:
        self.current = start

    def __call__(self) -> datetime:
        return self.current

    def advance(self, **delta: float) -> None:
        self.current = self.current + timedelta(**delta)

    def at(self, offset: timedelta) -> None:
        self.current = T0 + offset


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def service(db_session: AsyncSession, clock: FakeClock) -> TokenService:
    return TokenService(db_session, clock=clock)


@pytest.fixture
async def file_sessions(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'tokens.db'}", connect_args={"timeout": 60}
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def make_principal(
    db: AsyncSession,
    username: str,
    *,
    role: UserRole = UserRole.PUBLISHER,
    active: bool = True,
) -> Principal:
    user = await user_repo.create_user(
        db,
        username=username,
        email=f"{username}@example.com",
        password="password123",
        role=role,
    )
    principal = Principal(user_id=user.id, username=user.username, role=user.role)
    if not active:
        await user_repo.deactivate_user(db, user)
    await db.commit()
    return principal


@pytest.fixture
async def alice(db_session: AsyncSession) -> Principal:
    return await make_principal(db_session, "alice")


@pytest.fixture
async def bob(db_session: AsyncSession) -> Principal:
    return await make_principal(db_session, "bob")


@pytest.fixture
async def root(db_session: AsyncSession) -> Principal:
    return await make_principal(db_session, "root", role=UserRole.ADMIN)


def reading(principal: Principal) -> Principal:
    return replace(principal, scope=TokenScope.READ)


async def deactivate(db: AsyncSession, principal: Principal) -> None:
    user = await user_repo.get_user(db, principal.user_id)
    await user_repo.deactivate_user(db, user)
    await db.commit()


async def set_role(db: AsyncSession, principal: Principal, role: UserRole) -> None:
    user = await user_repo.get_user(db, principal.user_id)
    user.role = role
    await db.commit()


async def count_tokens(db: AsyncSession) -> int:
    return (await db.execute(select(func.count()).select_from(ApiToken))).scalar_one()


async def stored_column(db: AsyncSession, column):
    return (await db.execute(select(column))).scalar_one()


async def every_stored_value(db: AsyncSession) -> list[str]:
    values: list[str] = []
    for table in Base.metadata.sorted_tables:
        for row in (await db.execute(select(table))).all():
            values.extend("" if value is None else str(value) for value in row)
    return values


def other_token_like(raw: str) -> str:
    index = len("alx_") + 10
    replacement = "A" if raw[index] != "A" else "B"
    return raw[:index] + replacement + raw[index + 1 :]


async def test_create_returns_the_clear_token_in_the_documented_format(service, alice):
    created = await service.create(alice, TokenCreate(name="ci"))

    assert isinstance(created, TokenCreated)
    assert TOKEN_FORMAT.fullmatch(created.token)
    assert created.prefix == created.token[:12]
    assert uuid.UUID(created.id).version == 4
    assert created.name == "ci"
    assert created.created_at.tzinfo is not None
    assert created.last_used_at is None
    assert created.revoked_at is None


async def test_create_draws_32_random_bytes_in_urlsafe_encoding(service, alice, monkeypatch):
    requested = []

    def fake_token_urlsafe(nbytes=None):
        requested.append(nbytes)
        return "Q" * 43

    monkeypatch.setattr(
        token_service_module, "secrets", types.SimpleNamespace(token_urlsafe=fake_token_urlsafe)
    )

    created = await service.create(alice, TokenCreate(name="ci"))

    assert requested == [32]
    assert created.token == "alx_" + "Q" * 43
    assert created.prefix == "alx_QQQQQQQQ"


async def test_create_defaults_to_read_scope_without_expiry(service, alice):
    created = await service.create(alice, TokenCreate(name="ci"))

    assert created.scope == "read"
    assert created.expires_at is None


async def test_create_applies_scope_and_expiry_from_the_request(service, alice):
    created = await service.create(
        alice, TokenCreate(name="deploy", scope="write", expires_in_days=30)
    )

    assert created.scope == "write"
    assert created.expires_at == T0 + timedelta(days=30)


async def test_create_strips_the_name(service, alice):
    created = await service.create(alice, TokenCreate(name="  ci runner  "))

    assert created.name == "ci runner"


@pytest.mark.parametrize("name", [" ", "   ", "\t", "\n \t"])
async def test_create_rejects_blank_names(db_session, service, alice, name):
    with pytest.raises(ValidationError):
        await service.create(alice, TokenCreate(name=name))

    assert await count_tokens(db_session) == 0


async def test_database_holds_only_the_sha256_of_the_token(db_session, service, alice):
    created = await service.create(alice, TokenCreate(name="ci", scope="write"))

    row = (await db_session.execute(select(ApiToken.token_hash, ApiToken.prefix))).one()
    assert row.token_hash == hashlib.sha256(created.token.encode("utf-8")).hexdigest()
    assert row.prefix == created.token[:12]
    secret_part = created.token[len("alx_") :]
    for value in await every_stored_value(db_session):
        assert created.token not in value
        assert secret_part not in value


async def test_hash_token_is_the_sha256_hex_of_the_complete_token():
    assert hash_token("alx_abc") == hashlib.sha256(b"alx_abc").hexdigest()
    assert len(hash_token("alx_abc")) == 64


async def test_tokens_never_repeat(service, alice, bob):
    created = [await service.create(alice, TokenCreate(name=f"a{n}")) for n in range(12)]
    created += [await service.create(bob, TokenCreate(name=f"b{n}")) for n in range(12)]

    assert len({item.token for item in created}) == 24
    assert len({item.id for item in created}) == 24
    assert len({hash_token(item.token) for item in created}) == 24


async def test_create_commits_its_transaction(db_session, service, alice):
    await service.create(alice, TokenCreate(name="ci"))

    assert not db_session.in_transaction()


async def test_create_rolls_back_when_persisting_fails(db_session, service, alice, monkeypatch):
    original = token_repo.create_token

    async def flush_then_fail(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("storage went away")

    with monkeypatch.context() as patch:
        patch.setattr(token_repo, "create_token", flush_then_fail)
        with pytest.raises(RuntimeError):
            await service.create(alice, TokenCreate(name="ci"))

    assert not db_session.in_transaction()
    assert await count_tokens(db_session) == 0
    created = await service.create(alice, TokenCreate(name="retry"))
    assert await count_tokens(db_session) == 1
    assert created.name == "retry"


async def test_create_requires_a_write_scoped_principal(db_session, service, alice):
    with pytest.raises(ForbiddenError):
        await service.create(reading(alice), TokenCreate(name="sneaky", scope="write"))

    assert await count_tokens(db_session) == 0


async def test_create_requires_authentication(db_session, service):
    with pytest.raises(UnauthorizedError):
        await service.create(None, TokenCreate(name="anonymous"))

    assert await count_tokens(db_session) == 0


async def test_create_rejects_principals_whose_account_is_inactive_or_unknown(db_session, service):
    retired = await make_principal(db_session, "retired", active=False)
    ghost = Principal(user_id=str(uuid.uuid4()), username="ghost", role=UserRole.PUBLISHER)

    for principal in (retired, ghost):
        with pytest.raises(UnauthorizedError):
            await service.create(principal, TokenCreate(name="late"))

    assert await count_tokens(db_session) == 0


async def test_a_read_token_cannot_mint_manage_or_revoke_tokens(service, alice):
    reader_token = await service.create(alice, TokenCreate(name="reader", scope="read"))
    writer_token = await service.create(alice, TokenCreate(name="writer", scope="write"))
    reader = await service.verify(reader_token.token)
    assert reader is not None and not reader.can_write

    with pytest.raises(ForbiddenError):
        await service.create(reader, TokenCreate(name="escalate", scope="write"))
    with pytest.raises(ForbiddenError):
        await service.list(reader)
    with pytest.raises(ForbiddenError):
        await service.revoke(reader, writer_token.id)
    with pytest.raises(ForbiddenError):
        await service.revoke(reader, reader_token.id)

    assert await service.verify(writer_token.token) is not None
    assert await service.verify(reader_token.token) is not None
    assert len(await service.list(alice)) == 2


async def test_create_enforces_the_per_user_limit(db_session, clock, alice, monkeypatch):
    limited = TokenService(db_session, max_per_user=3, clock=clock)
    for index in range(3):
        await limited.create(alice, TokenCreate(name=f"t{index}"))
    inserted = []
    original = token_repo.create_token

    async def recording_insert(*args, **kwargs):
        inserted.append(kwargs.get("name"))
        return await original(*args, **kwargs)

    monkeypatch.setattr(token_repo, "create_token", recording_insert)

    with pytest.raises(ConflictError):
        await limited.create(alice, TokenCreate(name="one-too-many"))

    assert inserted == []
    assert await count_tokens(db_session) == 3


async def test_the_limit_is_per_user(db_session, clock, alice, bob):
    limited = TokenService(db_session, max_per_user=2, clock=clock)
    for index in range(2):
        await limited.create(alice, TokenCreate(name=f"t{index}"))

    with pytest.raises(ConflictError):
        await limited.create(alice, TokenCreate(name="blocked"))
    created = await limited.create(bob, TokenCreate(name="free"))

    assert created.name == "free"


async def test_the_limit_defaults_to_the_configured_value(db_session, clock, alice, monkeypatch):
    monkeypatch.setattr(settings.tokens, "max_per_user", 2)
    configured = TokenService(db_session, clock=clock)

    assert configured.max_per_user == 2
    await configured.create(alice, TokenCreate(name="one"))
    await configured.create(alice, TokenCreate(name="two"))
    with pytest.raises(ConflictError):
        await configured.create(alice, TokenCreate(name="three"))


async def test_an_explicit_limit_overrides_the_configured_value(
    db_session, clock, alice, monkeypatch
):
    monkeypatch.setattr(settings.tokens, "max_per_user", 1)
    explicit = TokenService(db_session, max_per_user=2, clock=clock)

    await explicit.create(alice, TokenCreate(name="one"))
    await explicit.create(alice, TokenCreate(name="two"))
    with pytest.raises(ConflictError):
        await explicit.create(alice, TokenCreate(name="three"))


async def test_the_default_limit_is_twenty_active_tokens(db_session, clock, alice, monkeypatch):
    monkeypatch.setattr(settings.tokens, "max_per_user", 20)
    configured = TokenService(db_session, clock=clock)

    for index in range(20):
        await configured.create(alice, TokenCreate(name=f"t{index}"))
    with pytest.raises(ConflictError):
        await configured.create(alice, TokenCreate(name="t20"))

    assert await count_tokens(db_session) == 20


async def test_revoked_and_expired_tokens_free_their_slot(db_session, clock, alice):
    limited = TokenService(db_session, max_per_user=2, clock=clock)
    await limited.create(alice, TokenCreate(name="short", expires_in_days=1))
    keeper = await limited.create(alice, TokenCreate(name="keeper"))
    with pytest.raises(ConflictError):
        await limited.create(alice, TokenCreate(name="third"))

    clock.advance(days=1)
    await limited.create(alice, TokenCreate(name="after-expiry"))
    with pytest.raises(ConflictError):
        await limited.create(alice, TokenCreate(name="fourth"))

    await limited.revoke(alice, keeper.id)
    await limited.create(alice, TokenCreate(name="after-revoke"))
    with pytest.raises(ConflictError):
        await limited.create(alice, TokenCreate(name="fifth"))


async def test_the_limit_is_checked_again_after_inserting(db_session, clock, alice, monkeypatch):
    limited = TokenService(db_session, max_per_user=1, clock=clock)
    await limited.create(alice, TokenCreate(name="first"))
    real_count = token_repo.count_active_tokens
    calls = []

    async def stale_then_real(db, user_id, **kwargs):
        calls.append(user_id)
        if len(calls) == 1:
            return 0
        return await real_count(db, user_id, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(token_repo, "count_active_tokens", stale_then_real)
        with pytest.raises(ConflictError):
            await limited.create(alice, TokenCreate(name="racer"))

    assert len(calls) == 2
    assert not db_session.in_transaction()
    assert await count_tokens(db_session) == 1


async def test_create_locks_the_owner_row_before_counting(db_session, service, alice):
    statements = []

    @event.listens_for(db_session.sync_session, "do_orm_execute")
    def capture(state):
        statements.append(str(state.statement.compile(dialect=postgresql.dialect())))

    await service.create(alice, TokenCreate(name="ci"))

    locking = [i for i, sql in enumerate(statements) if "FOR UPDATE" in sql]
    counting = [i for i, sql in enumerate(statements) if "count(" in sql.lower()]
    assert len(locking) == 1
    assert "FROM users" in statements[locking[0]]
    assert counting and locking[0] < counting[0]


async def test_concurrent_creations_cannot_exceed_the_limit(file_sessions):
    async with file_sessions() as setup:
        owner = await make_principal(setup, "alice")
        await TokenService(setup, max_per_user=4).create(owner, TokenCreate(name="existing"))

    async def attempt(index: int):
        async with file_sessions() as session:
            try:
                await TokenService(session, max_per_user=4).create(
                    owner, TokenCreate(name=f"racer-{index}")
                )
            except ConflictError as error:
                return error
            return None

    outcomes = await asyncio.gather(*(attempt(index) for index in range(10)))

    assert sum(outcome is None for outcome in outcomes) == 3
    assert all(isinstance(outcome, ConflictError) for outcome in outcomes if outcome is not None)
    async with file_sessions() as check:
        assert await count_tokens(check) == 4


async def test_concurrent_verifications_of_a_fresh_token_all_succeed(file_sessions):
    async with file_sessions() as setup:
        owner = await make_principal(setup, "alice")
        created = await TokenService(setup).create(owner, TokenCreate(name="ci", scope="write"))

    async def verify():
        async with file_sessions() as session:
            return await TokenService(session).verify(created.token)

    principals = await asyncio.gather(*(verify() for _ in range(20)))

    assert all(principal is not None for principal in principals)
    assert {principal.user_id for principal in principals} == {owner.user_id}
    async with file_sessions() as check:
        assert await stored_column(check, ApiToken.last_used_at) is not None


async def test_a_zero_limit_allows_no_tokens(db_session, clock, alice):
    closed = TokenService(db_session, max_per_user=0, clock=clock)

    with pytest.raises(ConflictError):
        await closed.create(alice, TokenCreate(name="none"))

    assert await count_tokens(db_session) == 0


async def test_verify_returns_the_principal_of_a_valid_token(service, alice):
    for scope in ("read", "write"):
        created = await service.create(alice, TokenCreate(name=scope, scope=scope))

        principal = await service.verify(created.token)

        assert principal == Principal(
            user_id=alice.user_id,
            username="alice",
            role=UserRole.PUBLISHER,
            scope=TokenScope(scope),
        )
        assert principal.can_write is (scope == "write")


async def test_verify_takes_the_scope_from_the_token_and_the_role_from_the_user(service, root):
    created = await service.create(root, TokenCreate(name="read-only admin", scope="read"))

    principal = await service.verify(created.token)

    assert principal.role is UserRole.ADMIN
    assert principal.is_admin
    assert principal.scope is TokenScope.READ
    assert not principal.can_write


async def test_verify_does_not_depend_on_the_service_instance_that_issued_the_token(
    db_session, service, alice
):
    created = await service.create(alice, TokenCreate(name="ci", scope="write"))

    principal = await TokenService(db_session).verify(created.token)

    assert principal is not None
    assert principal.user_id == alice.user_id


async def test_verify_reads_the_role_from_the_user_on_every_call(db_session, service, alice):
    created = await service.create(alice, TokenCreate(name="ci", scope="write"))
    assert (await service.verify(created.token)).role is UserRole.PUBLISHER

    await set_role(db_session, alice, UserRole.ADMIN)
    assert (await service.verify(created.token)).role is UserRole.ADMIN

    await set_role(db_session, alice, UserRole.PUBLISHER)
    assert (await service.verify(created.token)).role is UserRole.PUBLISHER


async def test_a_demoted_admin_loses_admin_rights_through_tokens_issued_before(
    db_session, service, alice, root
):
    victim_token = await service.create(alice, TokenCreate(name="ci", scope="write"))
    admin_token = await service.create(root, TokenCreate(name="ops", scope="write"))
    admin_principal = await service.verify(admin_token.token)
    await service.revoke(admin_principal, victim_token.id)
    other_token = await service.create(alice, TokenCreate(name="other", scope="write"))

    await set_role(db_session, root, UserRole.PUBLISHER)
    demoted = await service.verify(admin_token.token)

    assert not demoted.is_admin
    with pytest.raises(NotFoundError):
        await service.revoke(demoted, other_token.id)
    assert await service.verify(other_token.token) is not None


async def test_verify_rejects_a_revoked_token(service, alice):
    created = await service.create(alice, TokenCreate(name="ci", scope="write"))
    assert await service.verify(created.token) is not None

    await service.revoke(alice, created.id)

    assert await service.verify(created.token) is None


async def test_verify_honours_the_expiry_instant_with_an_injected_clock(service, clock, alice):
    created = await service.create(alice, TokenCreate(name="short", expires_in_days=7))

    clock.advance(days=7, microseconds=-1)
    assert await service.verify(created.token) is not None
    clock.advance(microseconds=1)
    assert await service.verify(created.token) is None
    clock.advance(days=1)
    assert await service.verify(created.token) is None


async def test_a_token_without_expiry_never_expires(service, clock, alice):
    created = await service.create(alice, TokenCreate(name="forever"))

    clock.advance(days=3650 * 3)

    assert await service.verify(created.token) is not None


async def test_verify_rejects_tokens_of_users_deactivated_after_issuance(
    db_session, service, alice
):
    created = await service.create(alice, TokenCreate(name="ci", scope="write"))
    assert await service.verify(created.token) is not None

    await deactivate(db_session, alice)

    assert await service.verify(created.token) is None
    assert await stored_column(db_session, ApiToken.revoked_at) is None


async def test_verify_rejects_a_token_whose_owner_no_longer_exists(service, alice, monkeypatch):
    created = await service.create(alice, TokenCreate(name="ci"))

    async def vanished(db, user_id):
        return None

    monkeypatch.setattr(user_repo, "get_user", vanished)

    assert await service.verify(created.token) is None


async def test_verify_rejects_unknown_tokens_of_the_right_shape(service, alice):
    await service.create(alice, TokenCreate(name="ci"))

    assert await service.verify("alx_" + "A" * 43) is None
    assert await service.verify("alx_x") is None


async def test_verify_rejects_a_token_that_differs_in_a_single_character(service, alice):
    created = await service.create(alice, TokenCreate(name="ci", scope="write"))

    assert await service.verify(other_token_like(created.token)) is None
    assert await service.verify(created.token[:-1]) is None
    assert await service.verify(created.token + "A") is None
    assert await service.verify(created.token.swapcase()) is None
    assert await service.verify(created.token) is not None


async def test_the_prefix_is_only_a_label_and_never_authenticates(service, alice, monkeypatch):
    bodies = iter(["AAAAAAAA" + "x" * 35, "AAAAAAAA" + "y" * 35])
    monkeypatch.setattr(
        token_service_module,
        "secrets",
        types.SimpleNamespace(token_urlsafe=lambda nbytes=None: next(bodies)),
    )
    first = await service.create(alice, TokenCreate(name="first", scope="write"))
    second = await service.create(alice, TokenCreate(name="second", scope="write"))
    assert first.prefix == second.prefix == "alx_AAAAAAAA"

    assert await service.verify(first.token) is not None
    assert await service.verify(second.token) is not None
    assert await service.verify("alx_AAAAAAAA" + "z" * 35) is None
    await service.revoke(alice, first.id)
    assert await service.verify(first.token) is None
    assert await service.verify(second.token) is not None


async def test_a_repeated_token_value_is_rejected_instead_of_overwriting(
    db_session, service, alice, monkeypatch
):
    monkeypatch.setattr(
        token_service_module,
        "secrets",
        types.SimpleNamespace(token_urlsafe=lambda nbytes=None: "R" * 43),
    )
    first = await service.create(alice, TokenCreate(name="first", scope="write"))

    with pytest.raises(IntegrityError):
        await service.create(alice, TokenCreate(name="clone", scope="write"))

    assert not db_session.in_transaction()
    assert await count_tokens(db_session) == 1
    assert await service.verify(first.token) is not None


async def test_the_stored_hash_cannot_be_replayed_as_a_credential(db_session, service, alice):
    created = await service.create(alice, TokenCreate(name="ci", scope="write"))
    stored_hash = await stored_column(db_session, ApiToken.token_hash)

    for attempt in (stored_hash, "alx_" + stored_hash, created.id, created.prefix):
        assert await service.verify(attempt) is None


MALFORMED_CREDENTIALS = [
    pytest.param(None, id="none"),
    pytest.param("", id="empty"),
    pytest.param(" ", id="blank"),
    pytest.param("alx_", id="prefix-only"),
    pytest.param("alx", id="short-prefix"),
    pytest.param("ALX_" + "A" * 43, id="uppercase-prefix"),
    pytest.param("alx-" + "A" * 43, id="dash-instead-of-underscore"),
    pytest.param("xalx_" + "A" * 43, id="extra-leading-character"),
    pytest.param("tok_" + "A" * 43, id="foreign-prefix"),
    pytest.param("A" * 43, id="no-prefix"),
    pytest.param(" alx_" + "A" * 43, id="leading-space"),
    pytest.param("alx_" + "A" * 43 + " ", id="trailing-space"),
    pytest.param("alx_" + "A" * 43 + "\n", id="trailing-newline"),
    pytest.param("Bearer alx_" + "A" * 43, id="authorization-header-value"),
    pytest.param("alx_" + "A" * 10_000, id="oversized"),
    pytest.param("alx_" + "é" * 43, id="non-ascii"),
    pytest.param("alx_\x00" + "A" * 42, id="nul-byte"),
    pytest.param("alx_\ud800" + "A" * 42, id="lone-surrogate"),
    pytest.param("alx_" + "A" * 20 + " " + "A" * 22, id="inner-space"),
    pytest.param("alx_" + "A" * 20 + "." + "A" * 22, id="inner-dot"),
    pytest.param("alx_' OR '1'='1", id="sql-fragment"),
    pytest.param(12345, id="integer"),
    pytest.param(b"alx_" + b"A" * 43, id="bytes"),
    pytest.param(object(), id="object"),
]


@pytest.mark.parametrize("credential", MALFORMED_CREDENTIALS)
async def test_verify_rejects_malformed_credentials_without_raising_or_querying(
    service, credential, monkeypatch
):
    lookups = []

    async def forbidden_lookup(*args, **kwargs):
        lookups.append(args)
        raise AssertionError("a malformed credential reached the database")

    monkeypatch.setattr(token_repo, "get_active_token_by_hash", forbidden_lookup)

    assert await service.verify(credential) is None
    assert lookups == []


async def test_verify_looks_the_token_up_by_its_hash(service, alice, monkeypatch):
    created = await service.create(alice, TokenCreate(name="ci"))
    seen = []
    original = token_repo.get_active_token_by_hash

    async def spy(db, token_hash, **kwargs):
        seen.append(token_hash)
        return await original(db, token_hash, **kwargs)

    monkeypatch.setattr(token_repo, "get_active_token_by_hash", spy)

    assert await service.verify(created.token) is not None
    assert seen == [hashlib.sha256(created.token.encode("utf-8")).hexdigest()]


async def test_last_used_at_is_recorded_on_first_use(service, alice):
    created = await service.create(alice, TokenCreate(name="ci"))
    assert created.last_used_at is None

    await service.verify(created.token)

    [listed] = await service.list(alice)
    assert listed.last_used_at == T0


async def test_last_used_at_is_refreshed_at_most_every_ten_minutes(service, clock, alice):
    created = await service.create(alice, TokenCreate(name="ci"))
    assert LAST_USED_INTERVAL == timedelta(minutes=10)
    schedule = [
        (timedelta(seconds=0), timedelta(0)),
        (timedelta(seconds=1), timedelta(0)),
        (timedelta(minutes=5), timedelta(0)),
        (timedelta(minutes=9, seconds=59), timedelta(0)),
        (timedelta(minutes=10), timedelta(minutes=10)),
        (timedelta(minutes=10, seconds=1), timedelta(minutes=10)),
        (timedelta(minutes=19, seconds=59), timedelta(minutes=10)),
        (timedelta(minutes=20), timedelta(minutes=20)),
        (timedelta(hours=5), timedelta(hours=5)),
    ]

    for offset, expected in schedule:
        clock.at(offset)
        assert await service.verify(created.token) is not None
        [listed] = await service.list(alice)
        assert listed.last_used_at == T0 + expected, offset


async def test_last_used_at_is_tracked_per_token(service, clock, alice):
    first = await service.create(alice, TokenCreate(name="first"))
    second = await service.create(alice, TokenCreate(name="second"))

    clock.at(timedelta(minutes=3))
    await service.verify(first.token)

    listed = {item.id: item for item in await service.list(alice)}
    assert listed[first.id].last_used_at == T0 + timedelta(minutes=3)
    assert listed[second.id].last_used_at is None


async def test_the_usage_update_is_committed(db_session, service, alice):
    created = await service.create(alice, TokenCreate(name="ci"))

    await service.verify(created.token)

    assert not db_session.in_transaction()


async def test_a_revoked_token_does_not_get_last_used_at_updated(db_session, service, clock, alice):
    created = await service.create(alice, TokenCreate(name="ci"))
    await service.revoke(alice, created.id)
    clock.advance(minutes=30)

    assert await service.verify(created.token) is None

    assert await stored_column(db_session, ApiToken.last_used_at) is None


async def test_an_inactive_owner_does_not_get_last_used_at_updated(db_session, service, alice):
    created = await service.create(alice, TokenCreate(name="ci"))
    await deactivate(db_session, alice)

    assert await service.verify(created.token) is None

    assert await stored_column(db_session, ApiToken.last_used_at) is None


async def test_a_failure_recording_usage_does_not_block_authentication(
    db_session, service, alice, monkeypatch, caplog
):
    created = await service.create(alice, TokenCreate(name="ci", scope="write"))

    async def database_is_locked(*args, **kwargs):
        raise OperationalError("UPDATE api_tokens", {}, Exception("database is locked"))

    caplog.set_level(logging.DEBUG)
    with monkeypatch.context() as patch:
        patch.setattr(token_repo, "touch_last_used", database_is_locked)
        principal = await service.verify(created.token)

    assert principal is not None
    assert principal.user_id == alice.user_id
    assert not db_session.in_transaction()
    assert any("Could not record" in record.getMessage() for record in caplog.records)
    assert created.token not in caplog.text
    assert await service.verify(created.token) is not None
    assert await stored_column(db_session, ApiToken.last_used_at) is not None


async def test_list_returns_only_the_callers_tokens(service, alice, bob):
    mine = {(await service.create(alice, TokenCreate(name=f"a{n}"))).id for n in range(3)}
    theirs = {(await service.create(bob, TokenCreate(name=f"b{n}"))).id for n in range(2)}

    listed = await service.list(alice)

    assert {item.id for item in listed} == mine
    assert not {item.id for item in listed} & theirs
    assert all(type(item) is TokenResponse for item in listed)
    assert {item.id for item in await service.list(bob)} == theirs


async def test_list_is_empty_for_a_user_without_tokens(service, alice, bob):
    await service.create(bob, TokenCreate(name="b"))

    assert await service.list(alice) == []


async def test_list_includes_revoked_and_expired_tokens_with_their_state(service, clock, alice):
    live = await service.create(alice, TokenCreate(name="live"))
    revoked = await service.create(alice, TokenCreate(name="revoked"))
    expiring = await service.create(alice, TokenCreate(name="expiring", expires_in_days=1))
    await service.revoke(alice, revoked.id)
    clock.advance(days=2)

    listed = {item.id: item for item in await service.list(alice)}

    assert set(listed) == {live.id, revoked.id, expiring.id}
    assert listed[live.id].revoked_at is None
    assert listed[revoked.id].revoked_at == T0
    assert listed[expiring.id].expires_at == T0 + timedelta(days=1)
    assert listed[expiring.id].expires_at < clock.current


async def test_list_never_exposes_the_secret_or_the_hash(db_session, service, alice):
    created = await service.create(alice, TokenCreate(name="ci", scope="write"))
    stored_hash = await stored_column(db_session, ApiToken.token_hash)

    listed = await service.list(alice)

    for item in listed:
        assert set(item.model_dump()) == RESPONSE_FIELDS
        assert not hasattr(item, "token")
    dumped = json.dumps([item.model_dump(mode="json") for item in listed])
    assert created.token not in dumped
    assert created.token[len("alx_") :] not in dumped
    assert stored_hash not in dumped


async def test_list_requires_a_write_scoped_principal(service, alice):
    await service.create(alice, TokenCreate(name="ci"))

    with pytest.raises(ForbiddenError):
        await service.list(reading(alice))
    with pytest.raises(UnauthorizedError):
        await service.list(None)


async def test_an_admin_lists_only_their_own_tokens(service, alice, root):
    await service.create(alice, TokenCreate(name="alice-token"))
    own = await service.create(root, TokenCreate(name="root-token"))

    listed = await service.list(root)

    assert [item.id for item in listed] == [own.id]


async def test_revoke_invalidates_the_token_immediately(service, clock, alice):
    created = await service.create(alice, TokenCreate(name="ci", scope="write"))
    clock.advance(hours=1)

    await service.revoke(alice, created.id)

    assert await service.verify(created.token) is None
    [listed] = await service.list(alice)
    assert listed.revoked_at == T0 + timedelta(hours=1)


async def test_revoke_is_idempotent_and_keeps_the_first_revocation_time(
    service, clock, alice, caplog
):
    caplog.set_level(logging.INFO)
    created = await service.create(alice, TokenCreate(name="ci"))
    await service.revoke(alice, created.id)
    clock.advance(days=2)

    await service.revoke(alice, created.id)
    await service.revoke(alice, created.id)

    [listed] = await service.list(alice)
    assert listed.revoked_at == T0
    revocations = [r for r in caplog.records if r.getMessage() == "API token revoked"]
    assert len(revocations) == 1


async def test_revoke_commits_its_transaction(db_session, service, alice):
    created = await service.create(alice, TokenCreate(name="ci"))

    await service.revoke(alice, created.id)

    assert not db_session.in_transaction()


async def test_revoke_rolls_back_when_persisting_fails(db_session, service, alice, monkeypatch):
    created = await service.create(alice, TokenCreate(name="ci", scope="write"))
    original = token_repo.revoke_token

    async def flush_then_fail(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("storage went away")

    with monkeypatch.context() as patch:
        patch.setattr(token_repo, "revoke_token", flush_then_fail)
        with pytest.raises(RuntimeError):
            await service.revoke(alice, created.id)

    assert not db_session.in_transaction()
    assert await stored_column(db_session, ApiToken.revoked_at) is None
    assert await service.verify(created.token) is not None


async def test_revoking_someone_elses_token_is_not_found_and_changes_nothing(service, alice, bob):
    created = await service.create(alice, TokenCreate(name="ci", scope="write"))

    with pytest.raises(NotFoundError):
        await service.revoke(bob, created.id)

    assert await service.verify(created.token) is not None
    [listed] = await service.list(alice)
    assert listed.revoked_at is None


async def test_revoking_someone_elses_already_revoked_token_is_still_not_found(service, alice, bob):
    created = await service.create(alice, TokenCreate(name="ci", scope="write"))
    await service.revoke(alice, created.id)

    with pytest.raises(NotFoundError):
        await service.revoke(bob, created.id)

    [listed] = await service.list(alice)
    assert listed.revoked_at == T0


async def test_foreign_and_unknown_token_ids_are_indistinguishable(service, alice, bob):
    created = await service.create(alice, TokenCreate(name="ci"))

    with pytest.raises(NotFoundError) as foreign:
        await service.revoke(bob, created.id)
    with pytest.raises(NotFoundError) as unknown:
        await service.revoke(bob, str(uuid.uuid4()))

    assert str(foreign.value) == str(unknown.value)
    assert foreign.value.code == unknown.value.code == "NOT_FOUND"
    assert created.id not in str(foreign.value)


HOSTILE_TOKEN_IDS = [
    pytest.param("", id="empty"),
    pytest.param(" ", id="blank"),
    pytest.param("not-a-uuid", id="not-a-uuid"),
    pytest.param("../../etc/passwd", id="path-traversal"),
    pytest.param("' OR '1'='1", id="sql-fragment"),
    pytest.param("%", id="like-wildcard"),
    pytest.param("x" * 5000, id="oversized"),
    pytest.param("\x00", id="nul"),
    pytest.param("\ud800", id="lone-surrogate"),
    pytest.param(None, id="none"),
    pytest.param(12345, id="integer"),
]


@pytest.mark.parametrize("token_id", HOSTILE_TOKEN_IDS)
async def test_hostile_token_ids_are_not_found_and_destroy_nothing(
    db_session, service, alice, bob, token_id
):
    created = await service.create(alice, TokenCreate(name="ci", scope="write"))

    with pytest.raises(NotFoundError):
        await service.revoke(bob, token_id)

    assert await count_tokens(db_session) == 1
    assert await service.verify(created.token) is not None


async def test_an_admin_can_revoke_any_users_token(service, alice, root):
    created = await service.create(alice, TokenCreate(name="ci", scope="write"))

    await service.revoke(root, created.id)

    assert await service.verify(created.token) is None
    [listed] = await service.list(alice)
    assert listed.revoked_at is not None


async def test_an_admin_without_write_scope_cannot_revoke(service, alice, root):
    created = await service.create(alice, TokenCreate(name="ci", scope="write"))

    with pytest.raises(ForbiddenError):
        await service.revoke(reading(root), created.id)

    assert await service.verify(created.token) is not None


async def test_an_admin_read_token_cannot_revoke_other_users_tokens(service, alice, root):
    victim = await service.create(alice, TokenCreate(name="ci", scope="write"))
    admin_reader = await service.create(root, TokenCreate(name="audit", scope="read"))
    principal = await service.verify(admin_reader.token)
    assert principal.is_admin and not principal.can_write

    with pytest.raises(ForbiddenError):
        await service.revoke(principal, victim.id)

    assert await service.verify(victim.token) is not None


async def test_revoke_requires_authentication(service, alice):
    created = await service.create(alice, TokenCreate(name="ci"))

    with pytest.raises(UnauthorizedError):
        await service.revoke(None, created.id)

    assert await service.verify(created.token) is not None


async def test_the_default_clock_is_the_system_clock(db_session, alice):
    system = TokenService(db_session)
    before = datetime.now(UTC)

    created = await system.create(alice, TokenCreate(name="ci", expires_in_days=1))

    expected = before + timedelta(days=1)
    assert abs(created.expires_at - expected) < timedelta(minutes=1)
    assert await system.verify(created.token) is not None


async def test_clocks_returning_other_timezones_or_naive_values_are_read_as_utc(db_session, alice):
    minus_four = timezone(timedelta(hours=-4))
    zoned = TokenService(db_session, clock=lambda: datetime(2026, 3, 1, 8, 0, tzinfo=minus_four))
    naive = TokenService(db_session, clock=lambda: datetime(2026, 3, 1, 12, 0))

    first = await zoned.create(alice, TokenCreate(name="zoned", expires_in_days=1))
    second = await naive.create(alice, TokenCreate(name="naive", expires_in_days=1))

    assert first.expires_at == datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
    assert second.expires_at == datetime(2026, 3, 2, 12, 0, tzinfo=UTC)


async def test_neither_logs_nor_errors_contain_the_token(db_session, service, alice, bob, caplog):
    caplog.set_level(logging.DEBUG)
    created = await service.create(alice, TokenCreate(name="ci", scope="write"))
    raw = created.token
    errors: list[DomainError] = []

    async def collect(awaitable):
        try:
            await awaitable
        except DomainError as error:
            errors.append(error)

    await service.verify(raw)
    await service.verify(other_token_like(raw))
    await service.list(alice)
    await collect(service.revoke(bob, created.id))
    await collect(service.create(reading(alice), TokenCreate(name="denied", scope="write")))
    await collect(service.create(None, TokenCreate(name="anonymous")))
    await collect(service.revoke(alice, "no-such-token"))
    limited = TokenService(db_session, max_per_user=1)
    await collect(limited.create(alice, TokenCreate(name="over-limit")))
    await service.revoke(alice, created.id)
    await service.verify(raw)
    survivor = await service.create(bob, TokenCreate(name="survivor"))
    await deactivate(db_session, bob)
    await service.verify(survivor.token)

    assert len(errors) == 5
    formatter = JSONFormatter()
    everything = "\n".join(
        [caplog.text]
        + [formatter.format(record) for record in caplog.records]
        + [str(record.__dict__) for record in caplog.records]
        + [f"{error!r} {error} {error.message} {error.reason}" for error in errors]
    )
    application = "\n".join(
        formatter.format(record) + str(record.__dict__)
        for record in caplog.records
        if record.name.startswith("app.")
    )
    for token in (raw, survivor.token):
        assert token not in everything
        assert token[len("alx_") :] not in everything
        assert hash_token(token) not in application
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "API token created" in messages
    assert "API token revoked" in messages
    assert "owner is unavailable" in messages
