from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from jose import jwt
from passlib.exc import PasswordSizeError
from passlib.utils import MAX_PASSWORD_SIZE
from pydantic import ValidationError as SchemaValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.auth import local as local_module
from app.auth import password as password_module
from app.auth.local import LocalAuthProvider
from app.auth.password import MAX_PASSWORD_LENGTH, verify_password
from app.config.settings import settings
from app.db.base import Base
from app.models.schemas import LoginRequest, TokenCreate, UserCreate, UserResponse
from app.models.token import TokenScope
from app.models.user import User, UserRole
from app.repositories import user_repo
from app.services.auth_service import AuthService
from app.services.errors import (
    ConflictError,
    DomainError,
    UnauthorizedError,
    ValidationError,
)
from app.services.principal import Principal
from app.services.token_service import TokenService
from app.services.user_service import UserService

pytestmark = pytest.mark.usefixtures("fast_password_hashing")

SECRET = "unit-test-signing-secret"
T0 = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
OVERSIZED_PASSWORD_SIZES = [MAX_PASSWORD_LENGTH + 1, 4096, 4097, 5000, 100_000]


class FakeClock:
    def __init__(self, start: datetime = T0) -> None:
        self.current = start

    def __call__(self) -> datetime:
        return self.current

    def advance(self, **delta: float) -> None:
        self.current = self.current + timedelta(**delta)


class CountingProvider(LocalAuthProvider):
    def __init__(self) -> None:
        super().__init__(secret=SECRET, algorithm="HS256", expiry_minutes=60)
        self.lookups: list[object] = []

    async def get_active_user_from_token(self, db, token):
        self.lookups.append(token)
        return await super().get_active_user_from_token(db, token)


class CountingTokens(TokenService):
    def __init__(self, db: AsyncSession, clock: FakeClock) -> None:
        super().__init__(db, clock=clock)
        self.verified: list[object] = []

    async def verify(self, raw_token):
        self.verified.append(raw_token)
        return await super().verify(raw_token)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def provider() -> LocalAuthProvider:
    return LocalAuthProvider(secret=SECRET, algorithm="HS256", expiry_minutes=60)


@pytest.fixture
def tokens(db_session: AsyncSession, clock: FakeClock) -> TokenService:
    return TokenService(db_session, clock=clock)


@pytest.fixture
def auth_service(
    db_session: AsyncSession, provider: LocalAuthProvider, tokens: TokenService
) -> AuthService:
    return AuthService(db_session, auth_provider=provider, token_service=tokens)


@pytest.fixture
async def file_sessions(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'users.db'}", connect_args={"timeout": 60}
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


def new_user(
    username: str = "alice",
    *,
    role: str = "publisher",
    email: str | None = None,
    password: str = "password123",
) -> UserCreate:
    return UserCreate(
        username=username,
        email=email or f"{username}@example.com",
        password=password,
        role=role,
    )


@dataclass(frozen=True)
class Account:
    id: str
    username: str
    role: UserRole


async def register(
    service: AuthService, username: str = "alice", *, role: str = "publisher"
) -> Account:
    user = await service.create_user(new_user(username, role=role))
    return Account(id=user.id, username=user.username, role=UserRole(user.role))


def principal_of(account: Account, scope: TokenScope = TokenScope.WRITE) -> Principal:
    return Principal(user_id=account.id, username=account.username, role=account.role, scope=scope)


def principal_of_response(account: UserResponse) -> Principal:
    return Principal(user_id=account.id, username=account.username, role=UserRole(account.role))


ROOT = Principal(user_id="root-actor", username="root", role=UserRole.ADMIN)


async def deactivate(db_session: AsyncSession, user_id: str) -> UserResponse:
    return await UserService(db_session).deactivate(ROOT, user_id)


def b64url(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode("utf-8")).rstrip(b"=").decode("ascii")


def forge_jwt(
    subject: object,
    *,
    secret: str = SECRET,
    algorithm: str = "HS256",
    lifetime: timedelta = timedelta(minutes=5),
    claims: dict | None = None,
    with_expiry: bool = True,
) -> str:
    payload = dict(claims or {})
    if subject is not None:
        payload["sub"] = subject
    if with_expiry:
        payload["exp"] = datetime.now(UTC) + lifetime
    return jwt.encode(payload, secret, algorithm=algorithm)


def unsigned_jwt(subject: str) -> str:
    expiry = int((datetime.now(UTC) + timedelta(minutes=5)).timestamp())
    header = b64url({"alg": "none", "typ": "JWT"})
    return f"{header}.{b64url({'sub': subject, 'exp': expiry})}."


def hostile_jwts(user_id: str, admin_id: str) -> dict[str, str]:
    valid = forge_jwt(user_id)
    header, payload, signature = valid.split(".")
    middle = len(signature) // 2
    flipped = "A" if signature[middle] != "A" else "B"
    expiry = int((datetime.now(UTC) + timedelta(minutes=5)).timestamp())
    return {
        "expired": forge_jwt(user_id, lifetime=timedelta(minutes=-1)),
        "foreign-secret": forge_jwt(user_id, secret="another-secret"),
        "empty-secret": forge_jwt(user_id, secret=""),
        "alg-none": unsigned_jwt(user_id),
        "hs384": forge_jwt(user_id, algorithm="HS384"),
        "hs512": forge_jwt(user_id, algorithm="HS512"),
        "no-subject": forge_jwt(None),
        "no-expiry": forge_jwt(user_id, with_expiry=False),
        "infinite-expiry": forge_jwt(user_id, with_expiry=False, claims={"exp": float("inf")}),
        "negative-infinite-expiry": forge_jwt(
            user_id, with_expiry=False, claims={"exp": float("-inf")}
        ),
        "nan-expiry": forge_jwt(user_id, with_expiry=False, claims={"exp": float("nan")}),
        "numeric-subject": forge_jwt(12345),
        "list-subject": forge_jwt([user_id]),
        "empty-subject": forge_jwt(""),
        "unknown-user": forge_jwt(str(uuid.uuid4())),
        "tampered-signature": f"{header}.{payload}.{signature[:middle]}{flipped}{signature[middle + 1 :]}",
        "swapped-payload": f"{header}.{b64url({'sub': admin_id, 'exp': expiry})}.{signature}",
        "truncated": valid[:-10],
        "signature-removed": f"{header}.{payload}.",
        "extra-segment": f"{valid}.extra",
    }


HOSTILE_JWT_CASES = [
    "expired",
    "foreign-secret",
    "empty-secret",
    "alg-none",
    "hs384",
    "hs512",
    "no-subject",
    "no-expiry",
    "infinite-expiry",
    "negative-infinite-expiry",
    "nan-expiry",
    "numeric-subject",
    "list-subject",
    "empty-subject",
    "unknown-user",
    "tampered-signature",
    "swapped-payload",
    "truncated",
    "signature-removed",
    "extra-segment",
]


async def count_users(db: AsyncSession) -> int:
    return (await db.execute(select(func.count()).select_from(User))).scalar_one()


async def test_create_user(auth_service):
    user = await auth_service.create_user(
        UserCreate(username="newuser", email="new@test.com", password="password123")
    )
    assert user.username == "newuser"
    assert user.email == "new@test.com"
    assert user.is_active is True


async def test_create_user_stores_the_requested_role(auth_service):
    publisher = await register(auth_service, "pat")
    admin = await register(auth_service, "ada", role="admin")

    assert publisher.role is UserRole.PUBLISHER
    assert admin.role is UserRole.ADMIN


async def test_create_user_never_stores_the_clear_password(db_session, auth_service):
    secret = "Zx9-unique-Passw0rd!"
    created = await auth_service.create_user(new_user(password=secret))

    user = await user_repo.get_user(db_session, created.id)
    assert user.password_hash != secret
    assert verify_password(secret, user.password_hash)
    assert not verify_password(secret + "x", user.password_hash)
    stored = (await db_session.execute(select(User))).scalars().all()
    for row in stored:
        assert secret not in f"{row.username} {row.email} {row.password_hash}"


async def test_create_duplicate_user_raises(db_session, auth_service):
    await auth_service.create_user(
        UserCreate(username="dupuser", email="dup@test.com", password="password123")
    )
    with pytest.raises(ConflictError, match="already exists") as raised:
        await auth_service.create_user(
            UserCreate(username="dupuser", email="dup2@test.com", password="password123")
        )

    assert raised.value.code == "CONFLICT"
    assert await count_users(db_session) == 1


async def test_create_user_with_a_duplicate_email_conflicts_and_keeps_the_session_usable(
    db_session, auth_service
):
    await auth_service.create_user(new_user("first", email="shared@example.com"))

    with pytest.raises(ConflictError):
        await auth_service.create_user(new_user("second", email="shared@example.com"))

    assert not db_session.in_transaction()
    assert await count_users(db_session) == 1
    third = await auth_service.create_user(new_user("third"))
    assert third.username == "third"
    assert await count_users(db_session) == 2


async def test_concurrent_registrations_of_one_username_leave_a_single_user(file_sessions):
    async def attempt(index: int):
        async with file_sessions() as session:
            service = AuthService(session, auth_provider=LocalAuthProvider(secret=SECRET))
            try:
                await service.create_user(new_user("alice", email=f"alice-{index}@example.com"))
            except ConflictError as error:
                return error
            return None

    outcomes = await asyncio.gather(*(attempt(index) for index in range(6)))

    assert sum(outcome is None for outcome in outcomes) == 1
    assert all(isinstance(outcome, ConflictError) for outcome in outcomes if outcome is not None)
    async with file_sessions() as check:
        assert await count_users(check) == 1


@pytest.mark.parametrize(
    "role", ["root", "ADMIN", "Admin", "", " admin", "publisher ", "superuser"]
)
async def test_create_user_with_an_invalid_role_raises_validation_error(
    db_session, auth_service, role
):
    with pytest.raises(ValidationError, match="admin, publisher") as raised:
        await auth_service.create_user(new_user(role=role))

    assert raised.value.code == "VALIDATION_ERROR"
    assert await count_users(db_session) == 0


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"password": "pass\x00word123"}, id="nul-in-password"),
        pytest.param({"username": "ali\x00ce"}, id="nul-in-username"),
        pytest.param({"email": "ali\x00ce@example.com"}, id="nul-in-email"),
    ],
)
async def test_create_user_rejects_text_the_hasher_or_database_cannot_handle(
    db_session, auth_service, overrides
):
    data = new_user().model_copy(update=overrides)

    with pytest.raises(ValidationError):
        await auth_service.create_user(data)

    assert await count_users(db_session) == 0


async def test_create_user_rejects_unencodable_text_that_bypassed_schema_validation(
    db_session, auth_service
):
    data = UserCreate.model_construct(
        username="ali\ud800ce", email="a@example.com", password="password123", role="publisher"
    )

    with pytest.raises(ValidationError):
        await auth_service.create_user(data)

    assert await count_users(db_session) == 0


async def test_create_user_rolls_back_unexpected_failures(db_session, auth_service, monkeypatch):
    async def flush_then_fail(db, *, username, email, password, role):
        db.add(User(username=username, email=email, password_hash="unused", role=role))
        await db.flush()
        raise RuntimeError("storage went away")

    with monkeypatch.context() as patch:
        patch.setattr(user_repo, "create_user", flush_then_fail)
        with pytest.raises(RuntimeError):
            await auth_service.create_user(new_user())

    assert not db_session.in_transaction()
    assert await count_users(db_session) == 0
    assert (await auth_service.create_user(new_user())).username == "alice"


RESPONSE_FIELDS = {"id", "username", "email", "role", "is_active", "created_at"}


async def test_every_auth_operation_returns_response_dtos_never_orm_rows(auth_service):
    created = await auth_service.create_user(new_user())
    authenticated, _ = await auth_service.authenticate("alice", "password123")

    returned = [
        created,
        authenticated,
        await auth_service.get_account(principal_of_response(created)),
    ]

    assert all(type(item) is UserResponse for item in returned)
    assert not any(isinstance(item, User) for item in returned)
    for item in returned:
        assert set(item.model_dump()) == RESPONSE_FIELDS
        assert not hasattr(item, "password_hash")
        assert "$2" not in item.model_dump_json()


async def test_user_responses_carry_string_ids_lowercase_roles_and_aware_timestamps(auth_service):
    admin = await auth_service.create_user(new_user("ada", role="admin"))
    publisher = await auth_service.create_user(new_user("pat"))

    for item in (admin, publisher):
        assert type(item.id) is str
        assert uuid.UUID(item.id).version == 4
        assert type(item.role) is str
        assert item.created_at.utcoffset() == timedelta(0)
        assert item.model_dump(mode="json")["created_at"].endswith("Z")
    assert (admin.role, publisher.role) == ("admin", "publisher")
    assert UserResponse.model_json_schema()["properties"]["id"] == {"title": "Id", "type": "string"}


async def test_responses_stay_readable_when_the_session_expires_objects_on_commit(engine):
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=True)
    provider = LocalAuthProvider(secret=SECRET, algorithm="HS256", expiry_minutes=60)
    async with factory() as session:
        service = AuthService(session, auth_provider=provider)
        created = await service.create_user(new_user())
        authenticated, _ = await service.authenticate("alice", "password123")

    assert (created.username, created.email, created.is_active) == (
        "alice",
        "alice@example.com",
        True,
    )
    assert authenticated.id == created.id


async def test_get_account_returns_the_response_dto_of_the_principal(auth_service):
    user = await register(auth_service)

    account = await auth_service.get_account(principal_of(user))

    assert type(account) is UserResponse
    assert not isinstance(account, User)
    assert set(account.model_dump()) == RESPONSE_FIELDS
    assert (account.id, account.username, account.email) == (user.id, "alice", "alice@example.com")
    assert (account.role, account.is_active) == ("publisher", True)
    assert not hasattr(account, "password_hash")
    assert "$2" not in account.model_dump_json()


@pytest.mark.parametrize("scope", list(TokenScope))
async def test_get_account_describes_admins_for_any_credential_scope(auth_service, scope):
    admin = await register(auth_service, "root", role="admin")

    account = await auth_service.get_account(principal_of(admin, scope))

    assert (account.username, account.role) == ("root", "admin")


async def test_get_account_reads_the_current_row_not_the_claims_of_the_principal(auth_service):
    user = await register(auth_service)
    claims = Principal(user_id=user.id, username="mallory", role=UserRole.ADMIN)

    account = await auth_service.get_account(claims)

    assert (account.username, account.role) == ("alice", "publisher")


async def test_get_account_refuses_the_anonymous_caller(auth_service):
    with pytest.raises(UnauthorizedError) as raised:
        await auth_service.get_account(None)

    assert raised.value.code == "UNAUTHORIZED"


async def test_get_account_refuses_a_principal_whose_account_is_gone(auth_service):
    ghost = Principal(user_id=str(uuid.uuid4()), username="ghost", role=UserRole.PUBLISHER)

    with pytest.raises(UnauthorizedError) as raised:
        await auth_service.get_account(ghost)

    assert raised.value.message == "The account no longer exists"


async def test_get_account_refuses_a_principal_whose_account_was_deactivated(
    db_session, auth_service
):
    user = await register(auth_service)
    principal = principal_of(user)
    assert (await auth_service.get_account(principal)).is_active is True

    await deactivate(db_session, user.id)

    with pytest.raises(UnauthorizedError) as raised:
        await auth_service.get_account(principal)
    assert raised.value.message == "The account no longer exists"


async def test_get_account_does_not_change_anything(db_session, auth_service):
    user = await register(auth_service)

    await auth_service.get_account(principal_of(user))
    await auth_service.get_account(principal_of(user))

    assert not db_session.dirty and not db_session.new and not db_session.deleted
    assert await count_users(db_session) == 1


async def test_authenticate_success(auth_service):
    await auth_service.create_user(
        UserCreate(username="authuser", email="auth@test.com", password="securepass123")
    )
    user, token = await auth_service.authenticate("authuser", "securepass123")
    assert user.username == "authuser"
    assert token is not None
    assert len(token) > 0


async def test_authenticate_wrong_password(auth_service):
    await auth_service.create_user(
        UserCreate(username="wrongpw", email="wrong@test.com", password="correctpass")
    )
    with pytest.raises(UnauthorizedError, match="Invalid credentials") as raised:
        await auth_service.authenticate("wrongpw", "wrongpass")

    assert raised.value.code == "UNAUTHORIZED"


async def test_authenticate_nonexistent_user(auth_service):
    with pytest.raises(UnauthorizedError, match="Invalid credentials"):
        await auth_service.authenticate("nobody", "nopass")


async def test_authenticate_rejects_inactive_users_even_with_the_right_password(
    db_session, auth_service
):
    user = await auth_service.create_user(new_user())
    await deactivate(db_session, user.id)

    with pytest.raises(UnauthorizedError, match="Invalid credentials"):
        await auth_service.authenticate("alice", "password123")


async def test_authenticate_fails_closed_when_the_stored_hash_is_unusable(
    db_session, auth_service, caplog
):
    account = await register(auth_service)
    user = await user_repo.get_user(db_session, account.id)
    user.password_hash = "not-a-real-hash"
    await db_session.commit()
    caplog.set_level(logging.DEBUG)

    with pytest.raises(UnauthorizedError, match="Invalid credentials"):
        await auth_service.authenticate("alice", "password123")

    warnings = [r for r in caplog.records if r.getMessage() == "Stored password hash is not usable"]
    assert len(warnings) == 1
    assert warnings[0].name == "app.auth.local"
    assert "password123" not in str(warnings[0].__dict__)


async def test_authentication_failures_are_indistinguishable(db_session, auth_service):
    await auth_service.create_user(new_user("alice"))
    retired = await auth_service.create_user(new_user("retired"))
    await deactivate(db_session, retired.id)
    attempts = [
        ("alice", "wrong-password"),
        ("ghost", "password123"),
        ("retired", "password123"),
        ("alice", "pass\x00word123"),
        ("ghost", "pass\x00word123"),
        ("ali\x00ce", "password123"),
        ("ali\ud800ce", "password123"),
        ("alice", "pass\ud800word123"),
        ("alice", "p" * (MAX_PASSWORD_LENGTH + 1)),
        ("ghost", "p" * (MAX_PASSWORD_LENGTH + 1)),
        ("alice", "p" * 4097),
        ("ghost", "p" * 4097),
        ("alice", "p" * 100_000),
        ("ghost", "p" * 100_000),
        ("retired", "p" * 5000),
    ]
    failures = []

    for username, password in attempts:
        with pytest.raises(DomainError) as raised:
            await auth_service.authenticate(username, password)
        failures.append((type(raised.value), str(raised.value), raised.value.code))

    assert set(failures) == {(UnauthorizedError, "Invalid credentials", "UNAUTHORIZED")}


@pytest.mark.parametrize("size", OVERSIZED_PASSWORD_SIZES)
@pytest.mark.parametrize("username", ["alice", "ghost"])
async def test_oversized_passwords_fail_identically_for_known_and_unknown_users_without_hashing(
    auth_service, monkeypatch, username, size
):
    await auth_service.create_user(new_user("alice"))

    def forbidden(*args, **kwargs):
        raise AssertionError("an oversized password reached the password hasher")

    monkeypatch.setattr(local_module, "verify_password", forbidden)

    with pytest.raises(DomainError) as raised:
        await auth_service.authenticate(username, "p" * size)

    assert type(raised.value) is UnauthorizedError
    assert (str(raised.value), raised.value.code) == ("Invalid credentials", "UNAUTHORIZED")


@pytest.mark.parametrize("size", OVERSIZED_PASSWORD_SIZES)
@pytest.mark.parametrize("username", ["alice", "ghost"])
async def test_the_provider_returns_none_for_oversized_passwords_without_raising(
    db_session, auth_service, provider, username, size
):
    await auth_service.create_user(new_user("alice"))

    assert await provider.authenticate(db_session, username, "p" * size) is None


async def test_oversized_passwords_never_log_the_unusable_stored_hash_warning(auth_service, caplog):
    await register(auth_service)
    caplog.set_level(logging.DEBUG)

    for username in ("alice", "ghost"):
        with pytest.raises(UnauthorizedError):
            await auth_service.authenticate(username, "p" * 5000)

    assert [
        r for r in caplog.records if r.getMessage() == "Stored password hash is not usable"
    ] == []


@pytest.mark.parametrize("username", ["alice", "ghost"])
async def test_a_hasher_failure_is_swallowed_the_same_way_for_known_and_unknown_users(
    db_session, auth_service, provider, monkeypatch, username
):
    await auth_service.create_user(new_user("alice"))
    calls: list[str] = []

    def failing(plain, stored):
        calls.append(stored)
        raise PasswordSizeError(4096)

    monkeypatch.setattr(local_module, "verify_password", failing)

    assert await provider.authenticate(db_session, username, "password123") is None
    assert len(calls) == 1


@pytest.mark.parametrize("character", ["p", "ñ", "日", "\U0001f600"])
async def test_a_password_of_exactly_the_maximum_length_works_end_to_end(auth_service, character):
    password = "Q" + character * (MAX_PASSWORD_LENGTH - 1)
    wrong = "R" + character * (MAX_PASSWORD_LENGTH - 1)
    created = await auth_service.create_user(new_user(password=password))

    user, token = await auth_service.authenticate("alice", password)

    assert len(password) == MAX_PASSWORD_LENGTH
    assert user.id == created.id
    assert await auth_service.resolve_principal(token) is not None
    with pytest.raises(UnauthorizedError):
        await auth_service.authenticate("alice", wrong)


@pytest.mark.parametrize("size", OVERSIZED_PASSWORD_SIZES)
async def test_create_user_rejects_oversized_passwords_that_bypassed_schema_validation(
    db_session, auth_service, monkeypatch, size
):
    def forbidden(*args, **kwargs):
        raise AssertionError("an oversized password reached the password hasher")

    monkeypatch.setattr(user_repo, "hash_password", forbidden)
    data = UserCreate.model_construct(
        username="alice", email="alice@example.com", password="p" * size, role="publisher"
    )

    with pytest.raises(ValidationError, match=str(MAX_PASSWORD_LENGTH)) as raised:
        await auth_service.create_user(data)

    assert raised.value.code == "VALIDATION_ERROR"
    assert type(raised.value) is ValidationError
    assert await count_users(db_session) == 0


def test_the_user_schema_rejects_oversized_passwords_and_accepts_the_maximum():
    accepted = UserCreate(
        username="alice", email="alice@example.com", password="p" * MAX_PASSWORD_LENGTH
    )
    assert len(accepted.password) == MAX_PASSWORD_LENGTH
    for size in (MAX_PASSWORD_LENGTH + 1, 4097, 100_000):
        with pytest.raises(SchemaValidationError) as raised:
            UserCreate(username="alice", email="alice@example.com", password="p" * size)
        problem = raised.value.errors()[0]
        assert (problem["loc"], problem["type"]) == (("password",), "string_too_long")
    assert (
        UserCreate.model_json_schema()["properties"]["password"]["maxLength"] == MAX_PASSWORD_LENGTH
    )


def test_the_login_schema_rejects_oversized_passwords_and_accepts_the_maximum():
    accepted = LoginRequest(username="alice", password="p" * MAX_PASSWORD_LENGTH)
    assert len(accepted.password) == MAX_PASSWORD_LENGTH
    assert LoginRequest(username="alice", password="x").password == "x"
    for size in (MAX_PASSWORD_LENGTH + 1, 4097, 100_000):
        with pytest.raises(SchemaValidationError) as raised:
            LoginRequest(username="alice", password="p" * size)
        problem = raised.value.errors()[0]
        assert (problem["loc"], problem["type"]) == (("password",), "string_too_long")
    assert (
        LoginRequest.model_json_schema()["properties"]["password"]["maxLength"]
        == MAX_PASSWORD_LENGTH
    )


async def test_authenticate_returns_a_token_that_resolves_to_the_user(auth_service):
    created = await auth_service.create_user(new_user("alice", role="admin"))

    user, token = await auth_service.authenticate("alice", "password123")
    principal = await auth_service.resolve_principal(token)

    assert user.id == created.id
    assert principal == Principal(
        user_id=created.id, username="alice", role=UserRole.ADMIN, scope=TokenScope.WRITE
    )


async def test_authenticate_never_logs_the_password(auth_service, caplog):
    caplog.set_level(logging.DEBUG)
    secret = "Zx9-unique-Passw0rd!"
    await auth_service.create_user(new_user(password=secret))

    await auth_service.authenticate("alice", secret)
    for wrong in (secret + "x", secret[:-1]):
        with pytest.raises(UnauthorizedError):
            await auth_service.authenticate("alice", wrong)
    with pytest.raises(UnauthorizedError):
        await auth_service.authenticate("ghost", secret)

    app_records = [record for record in caplog.records if record.name.startswith("app.")]
    assert app_records
    for record in app_records:
        assert secret not in str(record.__dict__)
        assert secret not in record.getMessage()


async def test_default_construction_uses_the_configured_signing_settings(db_session, monkeypatch):
    monkeypatch.setattr(settings.auth, "jwt_secret", "configured-secret")
    service = AuthService(db_session)
    await service.create_user(new_user())

    user, token = await service.authenticate("alice", "password123")

    assert isinstance(service.auth_provider, LocalAuthProvider)
    assert isinstance(service.token_service, TokenService)
    assert jwt.decode(token, "configured-secret", algorithms=["HS256"])["sub"] == user.id
    principal = await service.resolve_principal(token)
    assert principal is not None and principal.user_id == user.id


async def test_resolve_principal_accepts_a_valid_jwt_with_write_scope(auth_service):
    user = await register(auth_service)

    principal = await auth_service.resolve_principal(forge_jwt(user.id))

    assert principal == principal_of(user)
    assert principal.can_write
    assert not principal.is_admin


async def test_resolve_principal_reads_the_role_from_the_user_not_from_the_claims(auth_service):
    user = await register(auth_service)
    elevated = forge_jwt(user.id, claims={"role": "admin"})

    principal = await auth_service.resolve_principal(elevated)

    assert principal.role is UserRole.PUBLISHER
    assert not principal.is_admin


@pytest.mark.parametrize("case", HOSTILE_JWT_CASES)
async def test_resolve_principal_rejects_hostile_jwts(auth_service, case):
    user = await register(auth_service)
    admin = await register(auth_service, "root", role="admin")
    hostile = hostile_jwts(user.id, admin.id)

    assert await auth_service.resolve_principal(forge_jwt(user.id)) is not None
    assert await auth_service.resolve_principal(hostile[case]) is None


async def test_the_hostile_jwt_cases_cover_every_builder(auth_service):
    assert set(hostile_jwts("u", "a")) == set(HOSTILE_JWT_CASES)


async def test_resolve_principal_rejects_the_jwt_of_a_deactivated_user(db_session, auth_service):
    user = await register(auth_service)
    _, token = await auth_service.authenticate("alice", "password123")
    assert await auth_service.resolve_principal(token) is not None

    await deactivate(db_session, user.id)

    assert await auth_service.resolve_principal(token) is None


async def test_resolve_principal_rejects_an_expired_login_token(db_session, tokens):
    expired_provider = LocalAuthProvider(secret=SECRET, algorithm="HS256", expiry_minutes=-1)
    service = AuthService(db_session, auth_provider=expired_provider, token_service=tokens)
    await service.create_user(new_user())

    _, token = await service.authenticate("alice", "password123")

    assert await service.resolve_principal(token) is None


async def test_resolve_principal_rejects_a_login_token_signed_by_another_installation(
    db_session, tokens
):
    other = LocalAuthProvider(secret="the-secret-of-another-server", algorithm="HS256")
    ours = LocalAuthProvider(secret=SECRET, algorithm="HS256")
    other_service = AuthService(db_session, auth_provider=other, token_service=tokens)
    our_service = AuthService(db_session, auth_provider=ours, token_service=tokens)
    await our_service.create_user(new_user())

    _, foreign_token = await other_service.authenticate("alice", "password123")

    assert await other_service.resolve_principal(foreign_token) is not None
    assert await our_service.resolve_principal(foreign_token) is None


@pytest.mark.parametrize(
    "bearer",
    [
        pytest.param(None, id="none"),
        pytest.param("", id="empty"),
        pytest.param(" ", id="blank"),
        pytest.param("garbage", id="garbage"),
        pytest.param("a.b.c", id="three-dots"),
        pytest.param("...", id="only-dots"),
        pytest.param("Bearer", id="scheme-only"),
        pytest.param("x" * 100_000, id="oversized"),
        pytest.param("\x00", id="nul"),
        pytest.param("eyJ\ud800.x.y", id="lone-surrogate"),
        pytest.param("é.é.é", id="non-ascii"),
        pytest.param(12345, id="integer"),
        pytest.param(b"eyJhbGciOiJIUzI1NiJ9.e30.x", id="bytes"),
        pytest.param(object(), id="object"),
    ],
)
async def test_resolve_principal_returns_none_for_garbage(auth_service, bearer):
    assert await auth_service.resolve_principal(bearer) is None


async def test_resolve_principal_requires_the_bare_credential_without_decoration(auth_service):
    user = await register(auth_service)
    valid = forge_jwt(user.id)

    assert await auth_service.resolve_principal(valid) is not None
    assert await auth_service.resolve_principal(f"Bearer {valid}") is None
    assert await auth_service.resolve_principal(f" {valid}") is None
    assert await auth_service.resolve_principal(f"{valid} ") is None
    assert await auth_service.resolve_principal(f"{valid}\n") is None
    assert await auth_service.resolve_principal(valid.upper()) is None


async def test_resolve_principal_accepts_valid_api_tokens_with_their_own_scope(
    auth_service, tokens
):
    user = await register(auth_service)
    owner = principal_of(user)
    reader = await tokens.create(owner, TokenCreate(name="reader", scope="read"))
    writer = await tokens.create(owner, TokenCreate(name="writer", scope="write"))

    read_principal = await auth_service.resolve_principal(reader.token)
    write_principal = await auth_service.resolve_principal(writer.token)

    assert read_principal == principal_of(user, TokenScope.READ)
    assert write_principal == principal_of(user, TokenScope.WRITE)
    assert not read_principal.can_write
    assert write_principal.can_write


async def test_resolve_principal_rejects_revoked_api_tokens(auth_service, tokens):
    user = await register(auth_service)
    owner = principal_of(user)
    created = await tokens.create(owner, TokenCreate(name="ci", scope="write"))
    assert await auth_service.resolve_principal(created.token) is not None

    await tokens.revoke(owner, created.id)

    assert await auth_service.resolve_principal(created.token) is None


async def test_resolve_principal_rejects_expired_api_tokens(auth_service, tokens, clock):
    user = await register(auth_service)
    created = await tokens.create(
        principal_of(user), TokenCreate(name="ci", scope="write", expires_in_days=1)
    )
    clock.advance(hours=23)
    assert await auth_service.resolve_principal(created.token) is not None

    clock.advance(hours=1)

    assert await auth_service.resolve_principal(created.token) is None


async def test_resolve_principal_rejects_api_tokens_of_deactivated_users(
    db_session, auth_service, tokens
):
    user = await register(auth_service)
    created = await tokens.create(principal_of(user), TokenCreate(name="ci", scope="write"))
    assert await auth_service.resolve_principal(created.token) is not None

    await deactivate(db_session, user.id)

    assert await auth_service.resolve_principal(created.token) is None


async def test_resolve_principal_rejects_unknown_and_altered_api_tokens(auth_service, tokens):
    user = await register(auth_service)
    created = await tokens.create(principal_of(user), TokenCreate(name="ci", scope="write"))
    altered = created.token[:-1] + ("A" if created.token[-1] != "A" else "B")

    assert await auth_service.resolve_principal("alx_" + "A" * 43) is None
    assert await auth_service.resolve_principal(altered) is None
    assert await auth_service.resolve_principal("ALX_" + created.token[4:]) is None
    assert await auth_service.resolve_principal(created.token) is not None


async def test_an_api_token_prefix_never_falls_back_to_jwt_verification(auth_service):
    user = await register(auth_service)
    valid_jwt = forge_jwt(user.id)

    assert await auth_service.resolve_principal(f"alx_{valid_jwt}") is None
    assert await auth_service.resolve_principal("alx_") is None


async def test_api_token_lifecycle_starting_from_a_login(auth_service, tokens):
    await register(auth_service)
    _, login_token = await auth_service.authenticate("alice", "password123")
    session = await auth_service.resolve_principal(login_token)

    created = await tokens.create(session, TokenCreate(name="agent", scope="read"))
    agent = await auth_service.resolve_principal(created.token)

    assert agent is not None
    assert agent.user_id == session.user_id
    assert agent.scope is TokenScope.READ
    assert [item.id for item in await tokens.list(session)] == [created.id]
    await tokens.revoke(session, created.id)
    assert await auth_service.resolve_principal(created.token) is None
    assert await auth_service.resolve_principal(login_token) is not None


async def test_each_credential_kind_reaches_only_its_own_verifier(db_session, clock):
    provider = CountingProvider()
    tokens = CountingTokens(db_session, clock)
    service = AuthService(db_session, auth_provider=provider, token_service=tokens)
    user = await register(service)
    _, login_token = await service.authenticate("alice", "password123")
    created = await tokens.create(principal_of(user), TokenCreate(name="ci"))

    await service.resolve_principal(created.token)
    assert provider.lookups == []
    assert tokens.verified == [created.token]

    await service.resolve_principal(login_token)
    assert provider.lookups == [login_token]
    assert tokens.verified == [created.token]

    for junk in (None, "", 123):
        await service.resolve_principal(junk)
    assert provider.lookups == [login_token]
    assert tokens.verified == [created.token]


def test_provider_defaults_come_from_the_settings(monkeypatch):
    monkeypatch.setattr(settings.auth, "jwt_secret", "configured-secret")
    monkeypatch.setattr(settings.auth, "jwt_algorithm", "HS512")
    monkeypatch.setattr(settings.auth, "jwt_expiry_minutes", 7)

    configured = LocalAuthProvider()

    assert configured.secret == "configured-secret"
    assert configured.algorithm == "HS512"
    assert configured.expiry_minutes == 7


def test_provider_arguments_override_the_settings(monkeypatch):
    monkeypatch.setattr(settings.auth, "jwt_secret", "configured-secret")

    explicit = LocalAuthProvider(secret="explicit", algorithm="HS384", expiry_minutes=3)

    assert (explicit.secret, explicit.algorithm, explicit.expiry_minutes) == (
        "explicit",
        "HS384",
        3,
    )


async def test_create_token_carries_subject_role_and_expiry(db_session, auth_service, provider):
    created = await auth_service.create_user(new_user(role="admin"))
    user = await user_repo.get_user(db_session, created.id)
    before = datetime.now(UTC)

    token = await provider.create_token(user)

    claims = jwt.decode(token, SECRET, algorithms=["HS256"])
    assert claims["sub"] == user.id
    assert claims["role"] == "admin"
    expiry = datetime.fromtimestamp(claims["exp"], UTC)
    assert timedelta(minutes=59) < expiry - before < timedelta(minutes=61)


async def test_get_user_from_token_still_returns_inactive_users(db_session, auth_service, provider):
    created = await auth_service.create_user(new_user())
    user = await user_repo.get_user(db_session, created.id)
    token = await provider.create_token(user)
    await deactivate(db_session, user.id)

    assert (await provider.get_user_from_token(db_session, token)).id == user.id
    assert await provider.get_active_user_from_token(db_session, token) is None


async def test_get_active_user_from_token_returns_active_users(db_session, auth_service, provider):
    created = await auth_service.create_user(new_user())
    user = await user_repo.get_user(db_session, created.id)
    token = await provider.create_token(user)

    resolved = await provider.get_active_user_from_token(db_session, token)

    assert resolved is not None
    assert resolved.id == user.id


@pytest.mark.parametrize(
    "token",
    [
        pytest.param("", id="empty"),
        pytest.param("garbage", id="garbage"),
        pytest.param("eyJ\ud800.x.y", id="lone-surrogate"),
        pytest.param("é.é.é", id="non-ascii"),
        pytest.param(None, id="none"),
        pytest.param(12345, id="integer"),
    ],
)
async def test_provider_lookups_return_none_for_unparseable_tokens(db_session, provider, token):
    assert await provider.get_user_from_token(db_session, token) is None
    assert await provider.get_active_user_from_token(db_session, token) is None


async def test_provider_swallows_unexpected_library_errors(db_session, provider, monkeypatch):
    def explode(*args, **kwargs):
        raise ValueError("the library changed its mind")

    monkeypatch.setattr(local_module.jwt, "decode", explode)

    assert await provider.get_user_from_token(db_session, "a.b.c") is None
    assert await provider.get_active_user_from_token(db_session, "a.b.c") is None


async def test_provider_ignores_tokens_without_subject_or_expiry(
    db_session, auth_service, provider
):
    user = await auth_service.create_user(new_user())

    assert await provider.get_user_from_token(db_session, forge_jwt(None)) is None
    assert (
        await provider.get_user_from_token(db_session, forge_jwt(user.id, with_expiry=False))
        is None
    )
    assert await provider.get_user_from_token(db_session, forge_jwt(user.id)) is not None


async def test_authenticate_verifies_a_decoy_hash_for_unknown_users(db_session, monkeypatch):
    verified: list[str] = []
    hashed: list[str] = []
    real_verify = local_module.verify_password
    real_hash = local_module.hash_password

    def spy_verify(plain, stored):
        verified.append(stored)
        return real_verify(plain, stored)

    def spy_hash(plain):
        hashed.append(plain)
        return real_hash(plain)

    monkeypatch.setattr(local_module, "verify_password", spy_verify)
    monkeypatch.setattr(local_module, "hash_password", spy_hash)
    local_module._decoy_password_hash.cache_clear()
    built = LocalAuthProvider(secret=SECRET, algorithm="HS256", expiry_minutes=60)
    assert len(hashed) == 1

    assert await built.authenticate(db_session, "ghost", "whatever-password") is None
    assert await built.authenticate(db_session, "ghost", "another-password") is None

    assert len(verified) == 2
    assert verified[0] == verified[1]
    assert verified[0].startswith("$2")
    assert len(hashed) == 1


async def test_authenticate_skips_hashing_for_unusable_text(
    db_session, auth_service, provider, monkeypatch
):
    await auth_service.create_user(new_user())

    def forbidden(*args, **kwargs):
        raise AssertionError("unusable text reached the password hasher")

    monkeypatch.setattr(local_module, "verify_password", forbidden)

    assert await provider.authenticate(db_session, "alice", "pass\x00word123") is None
    assert await provider.authenticate(db_session, "alice", "pass\ud800word123") is None
    assert await provider.authenticate(db_session, "ali\x00ce", "password123") is None
    assert await provider.authenticate(db_session, "ali\ud800ce", "password123") is None
    assert await provider.authenticate(db_session, None, "password123") is None


async def test_authenticate_rejects_inactive_users_at_the_provider(
    db_session, auth_service, provider
):
    user = await auth_service.create_user(new_user())
    await deactivate(db_session, user.id)

    assert await provider.authenticate(db_session, "alice", "password123") is None


def test_is_valid_password_enforces_type_text_validity_and_length():
    assert local_module.is_valid_password("pässwörd✓ 日本語")
    assert local_module.is_valid_password("p" * MAX_PASSWORD_LENGTH)
    assert local_module.is_valid_password("\U0001f600" * MAX_PASSWORD_LENGTH)
    assert not local_module.is_valid_password("p" * (MAX_PASSWORD_LENGTH + 1))
    assert not local_module.is_valid_password("a\x00b")
    assert not local_module.is_valid_password("a\ud800b")
    assert not local_module.is_valid_password(None)
    assert not local_module.is_valid_password(b"bytes")
    assert not local_module.is_valid_password(12345)


def test_the_maximum_password_length_has_one_definition_below_the_hasher_limit():
    assert local_module.MAX_PASSWORD_LENGTH is password_module.MAX_PASSWORD_LENGTH
    assert MAX_PASSWORD_LENGTH == 1024
    assert MAX_PASSWORD_LENGTH <= MAX_PASSWORD_SIZE
