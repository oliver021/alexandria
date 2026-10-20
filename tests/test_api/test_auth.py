from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from jose import jwt
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.auth.bearer import DUPLICATE_HEADER_MESSAGE, MALFORMED_HEADER_MESSAGE
from app.auth.password import MAX_PASSWORD_LENGTH
from app.config.settings import settings
from app.models.schemas import TokenCreate, UserCreate, UserResponse
from app.models.token import ApiToken
from app.repositories import user_repo
from app.services.auth_service import AuthService
from app.services.errors import UnauthorizedError
from app.services.token_service import TokenService
from app.services.user_service import UserService
from tests.helpers.factories import Actors
from tests.test_api.support import (
    JWT_SECRET,
    Api,
    Catalog,
    assert_error,
)

PASSWORD = "correct horse battery"
USER_FIELDS = {"id", "username", "email", "role", "is_active", "created_at"}
BAD_AUTHORIZATION_HEADERS = [
    "Basic dXNlcjpwYXNz",
    "Digest username=x",
    "Token abc",
    "Bearer",
    "Bearer ",
    "Bearer a b",
    "Bearer abc def ghi",
    "bearer",
    "Bearertoken",
    "",
    " ",
    "Negotiate abc",
]


async def make_login_user(
    db: AsyncSession, username: str = "carol", *, role: str = "publisher"
) -> str:
    created = await AuthService(db).create_user(
        UserCreate(username=username, email=f"{username}@example.com", password=PASSWORD, role=role)
    )
    return created.id


async def login(api: Api, username: str = "carol", password: str = PASSWORD):
    return await api.post("/auth/login", json={"username": username, "password": password})


def forge_jwt(subject: str, *, exp: datetime | None = None, secret: str = JWT_SECRET) -> str:
    claims = {
        "sub": subject,
        "role": "admin",
        "exp": exp or datetime.now(UTC) + timedelta(minutes=5),
    }
    return jwt.encode(claims, secret, algorithm="HS256")


@pytest.mark.asyncio
async def test_login_returns_a_jwt_and_the_user(api: Api, db_session: AsyncSession):
    user_id = await make_login_user(db_session)

    response = await login(api)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"token", "user"}
    assert set(body["user"]) == USER_FIELDS
    assert body["user"]["id"] == user_id
    assert body["user"]["username"] == "carol"
    assert body["user"]["email"] == "carol@example.com"
    assert body["user"]["role"] == "publisher"
    assert body["user"]["is_active"] is True
    claims = jwt.decode(body["token"], JWT_SECRET, algorithms=["HS256"])
    assert claims["sub"] == user_id
    assert "password" not in response.text.lower().replace("password_hash", "")
    assert "password_hash" not in response.text


@pytest.mark.asyncio
async def test_the_login_response_is_never_cached(api: Api, db_session: AsyncSession):
    await make_login_user(db_session)

    response = await login(api)

    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_the_jwt_from_login_authenticates_later_requests(api: Api, db_session: AsyncSession):
    await make_login_user(db_session)
    token = (await login(api)).json()["token"]

    me = await api.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert me.status_code == 200
    assert me.json()["username"] == "carol"


@pytest.mark.asyncio
async def test_the_jwt_from_login_carries_write_scope(api: Api, db_session: AsyncSession):
    await make_login_user(db_session)
    token = (await login(api)).json()["token"]

    created = await api.post(
        "/tokens",
        json={"name": "from-jwt"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert created.status_code == 201


@pytest.mark.asyncio
async def test_a_wrong_password_and_an_unknown_user_are_indistinguishable(
    api: Api, db_session: AsyncSession
):
    await make_login_user(db_session)

    wrong = await login(api, password="not the password")
    unknown = await login(api, username="nobody")

    wrong_error = assert_error(wrong, 401, "UNAUTHORIZED")
    unknown_error = assert_error(unknown, 401, "UNAUTHORIZED")
    assert wrong_error == unknown_error


@pytest.mark.asyncio
async def test_an_inactive_user_cannot_log_in(api: Api, db_session: AsyncSession, people: Actors):
    user_id = await make_login_user(db_session)
    await UserService(db_session).deactivate(people.admin, user_id)

    response = await login(api)

    assert_error(response, 401, "UNAUTHORIZED")


@pytest.mark.asyncio
async def test_a_password_over_the_maximum_is_rejected_by_validation(api: Api):
    response = await login(api, password="x" * (MAX_PASSWORD_LENGTH + 1))

    error = assert_error(response, 422, "VALIDATION_ERROR")
    assert "password" in error["message"]


@pytest.mark.asyncio
async def test_a_password_at_the_maximum_is_just_a_wrong_password(api: Api):
    response = await login(api, password="x" * MAX_PASSWORD_LENGTH)

    assert_error(response, 401, "UNAUTHORIZED")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({}, "body.username"),
        ({"username": "carol"}, "body.password"),
        ({"password": PASSWORD}, "body.username"),
        ({"username": 5, "password": PASSWORD}, "body.username"),
        ({"username": "carol", "password": None}, "body.password"),
    ],
)
async def test_a_malformed_login_names_the_offending_field(api: Api, payload, field):
    response = await api.post("/auth/login", json=payload)

    error = assert_error(response, 422, "VALIDATION_ERROR")
    assert field in error["message"]


@pytest.mark.asyncio
async def test_a_login_body_that_is_not_json_is_a_validation_error(api: Api):
    response = await api.post(
        "/auth/login", content=b"{nope", headers={"Content-Type": "application/json"}
    )

    assert_error(response, 422, "VALIDATION_ERROR")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "authorization", ["Bearer garbage", "Basic dXNlcjpwYXNz", "Bearer", "Token a b c"]
)
async def test_login_ignores_whatever_authorization_header_comes_with_it(
    api: Api, db_session: AsyncSession, authorization: str
):
    await make_login_user(db_session)

    response = await api.post(
        "/auth/login",
        json={"username": "carol", "password": PASSWORD},
        headers={"Authorization": authorization},
    )

    assert response.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("actor", "username", "role"),
    [
        ("owner", "alice", "publisher"),
        ("owner_read", "alice", "publisher"),
        ("stranger", "bob", "publisher"),
        ("admin", "root", "admin"),
        ("admin_read", "root", "admin"),
    ],
)
async def test_me_describes_the_user_behind_an_api_token(
    api: Api, actor: str, username: str, role: str
):
    response = await api.get("/auth/me", actor)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == USER_FIELDS
    assert body["username"] == username
    assert body["role"] == role
    assert body["is_active"] is True
    assert isinstance(body["id"], str)
    assert body["email"] == f"{username}@example.com"


@pytest.mark.asyncio
async def test_me_is_a_thin_adapter_over_the_auth_service(
    api: Api, people: Actors, monkeypatch: pytest.MonkeyPatch
):
    received: list[object] = []
    stub = UserResponse(
        id="11111111-2222-4333-8444-555555555555",
        username="stubbed",
        email="stubbed@example.com",
        role="admin",
        is_active=True,
        created_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
    )

    async def fake(self, principal):
        received.append(principal)
        return stub

    monkeypatch.setattr(AuthService, "get_account", fake)

    response = await api.get("/auth/me", "owner")

    assert response.status_code == 200
    assert response.json() == {
        "id": stub.id,
        "username": "stubbed",
        "email": "stubbed@example.com",
        "role": "admin",
        "is_active": True,
        "created_at": "2026-01-02T03:04:05Z",
    }
    assert received == [people.owner]


@pytest.mark.asyncio
async def test_me_reports_the_refusal_of_the_auth_service_as_a_401(
    api: Api, monkeypatch: pytest.MonkeyPatch
):
    async def refuse(self, principal):
        raise UnauthorizedError("The account no longer exists")

    monkeypatch.setattr(AuthService, "get_account", refuse)

    response = await api.get("/auth/me", "owner")

    error = assert_error(response, 401, "UNAUTHORIZED")
    assert error["message"] == "The account no longer exists"


@pytest.mark.asyncio
async def test_me_requires_a_credential(api: Api):
    response = await api.get("/auth/me")

    assert_error(response, 401, "UNAUTHORIZED")


@pytest.mark.asyncio
@pytest.mark.parametrize("authorization", BAD_AUTHORIZATION_HEADERS)
async def test_a_malformed_authorization_header_is_refused_even_when_reading_is_public(
    api: Api, catalog: Catalog, authorization: str
):
    anonymous = await api.get("/skills")
    response = await api.get("/skills", headers={"Authorization": authorization})

    assert anonymous.status_code == 200
    assert_error(response, 401, "UNAUTHORIZED")


@pytest.mark.asyncio
@pytest.mark.parametrize("authorization", BAD_AUTHORIZATION_HEADERS)
async def test_every_malformed_authorization_header_gets_the_message_of_the_shared_parser(
    api: Api, authorization: str
):
    response = await api.get("/auth/me", headers={"Authorization": authorization})

    error = assert_error(response, 401, "UNAUTHORIZED")
    assert error["message"] == MALFORMED_HEADER_MESSAGE


@pytest.mark.asyncio
async def test_two_authorization_headers_get_the_message_of_the_shared_parser(api: Api, tokens):
    response = await api.client.get(
        "/api/v1/auth/me",
        headers=[
            ("Authorization", f"Bearer {tokens['owner']}"),
            ("Authorization", f"Bearer {tokens['owner']}"),
        ],
    )

    error = assert_error(response, 401, "UNAUTHORIZED")
    assert error["message"] == DUPLICATE_HEADER_MESSAGE


@pytest.mark.asyncio
async def test_the_rest_dependency_hands_the_raw_header_values_to_the_shared_parser(
    api: Api, tokens, monkeypatch: pytest.MonkeyPatch
):
    seen: list[list[str]] = []

    def spy(values):
        seen.append(list(values))

    monkeypatch.setattr(deps, "extract_bearer", spy)

    response = await api.get("/auth/me", headers={"Authorization": f"Bearer {tokens['owner']}"})

    assert_error(response, 401, "UNAUTHORIZED")
    assert seen == [[f"Bearer {tokens['owner']}"]]


def test_the_rest_dependency_no_longer_parses_the_header_itself():
    source = Path(deps.__file__).read_text(encoding="utf-8")

    assert "partition" not in source
    assert "extract_bearer" in source


@pytest.mark.asyncio
@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER", "BeArEr"])
async def test_the_bearer_scheme_is_case_insensitive(api: Api, tokens, scheme: str):
    response = await api.get("/auth/me", headers={"Authorization": f"{scheme} {tokens['owner']}"})

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_extra_spaces_after_the_scheme_are_tolerated(api: Api, tokens):
    response = await api.get("/auth/me", headers={"Authorization": f"Bearer    {tokens['owner']}"})

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_two_authorization_headers_are_refused(api: Api, tokens):
    response = await api.client.get(
        "/api/v1/skills",
        headers=[
            ("Authorization", f"Bearer {tokens['owner']}"),
            ("Authorization", f"Bearer {tokens['admin']}"),
        ],
    )

    assert_error(response, 401, "UNAUTHORIZED")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/skills",
        "/skills/pub-skill",
        "/skills/pub-skill/versions",
        "/skills/pub-skill/versions/latest",
        "/skills/pub-skill/versions/latest/entrypoint",
        "/skills/pub-skill/versions/latest/files",
        "/skills/pub-skill/versions/latest/files/docs/guide.md",
        "/skills/pub-skill/versions/latest/raw/docs/guide.md",
        "/skills/pub-skill/versions/latest/download",
        "/tags",
        "/categories",
    ],
)
async def test_an_invalid_credential_gets_401_on_every_read_route_even_when_reading_is_public(
    api: Api, catalog: Catalog, path: str
):
    anonymous = await api.get(path)
    invalid = await api.get(path, headers={"Authorization": "Bearer not-a-real-credential"})

    assert anonymous.status_code == 200
    assert_error(invalid, 401, "UNAUTHORIZED")


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/health", "/info"])
async def test_public_routes_do_not_look_at_the_credential(api: Api, path: str):
    response = await api.get(path, headers={"Authorization": "Bearer not-a-real-credential"})

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_an_expired_jwt_is_refused(api: Api, db_session: AsyncSession):
    user_id = await make_login_user(db_session)
    token = forge_jwt(user_id, exp=datetime.now(UTC) - timedelta(minutes=1))

    response = await api.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert_error(response, 401, "UNAUTHORIZED")


@pytest.mark.asyncio
async def test_a_jwt_without_expiry_is_refused(api: Api, db_session: AsyncSession):
    user_id = await make_login_user(db_session)
    token = jwt.encode({"sub": user_id, "role": "admin"}, JWT_SECRET, algorithm="HS256")

    response = await api.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert_error(response, 401, "UNAUTHORIZED")


@pytest.mark.asyncio
async def test_a_jwt_signed_with_another_secret_is_refused(api: Api, db_session: AsyncSession):
    user_id = await make_login_user(db_session)
    token = jwt.encode(
        {"sub": user_id, "exp": datetime.now(UTC) + timedelta(minutes=5)},
        "some-other-secret",
        algorithm="HS256",
    )

    response = await api.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert_error(response, 401, "UNAUTHORIZED")


@pytest.mark.asyncio
async def test_an_unsigned_jwt_is_refused(api: Api, db_session: AsyncSession):
    user_id = await make_login_user(db_session)
    header = "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0"
    claims = f'{{"sub":"{user_id}","exp":{int(datetime.now(UTC).timestamp()) + 600}}}'
    payload = base64.urlsafe_b64encode(claims.encode()).rstrip(b"=").decode()

    response = await api.get("/auth/me", headers={"Authorization": f"Bearer {header}.{payload}."})

    assert_error(response, 401, "UNAUTHORIZED")


@pytest.mark.asyncio
async def test_a_jwt_for_a_user_that_does_not_exist_is_refused(api: Api):
    token = forge_jwt("00000000-0000-4000-8000-000000000000")

    response = await api.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert_error(response, 401, "UNAUTHORIZED")


@pytest.mark.asyncio
async def test_a_jwt_stops_working_when_its_user_is_deactivated(
    api: Api, db_session: AsyncSession, people: Actors
):
    user_id = await make_login_user(db_session)
    token = (await login(api)).json()["token"]
    before = await api.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    await UserService(db_session).deactivate(people.admin, user_id)

    after = await api.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert before.status_code == 200
    assert_error(after, 401, "UNAUTHORIZED")


@pytest.mark.asyncio
async def test_a_revoked_api_token_is_refused(api: Api, db_session: AsyncSession, people: Actors):
    created = await TokenService(db_session).create(people.owner, TokenCreate(name="short-lived"))
    headers = {"Authorization": f"Bearer {created.token}"}
    before = await api.get("/auth/me", headers=headers)
    await TokenService(db_session).revoke(people.owner, created.id)

    after = await api.get("/auth/me", headers=headers)

    assert before.status_code == 200
    assert_error(after, 401, "UNAUTHORIZED")


@pytest.mark.asyncio
async def test_an_expired_api_token_is_refused(api: Api, db_session: AsyncSession, people: Actors):
    created = await TokenService(db_session).create(
        people.owner, TokenCreate(name="expiring", expires_in_days=1)
    )
    headers = {"Authorization": f"Bearer {created.token}"}
    before = await api.get("/auth/me", headers=headers)
    await db_session.execute(
        update(ApiToken)
        .where(ApiToken.id == created.id)
        .values(expires_at=datetime.now(UTC) - timedelta(hours=1))
    )
    await db_session.commit()

    after = await api.get("/auth/me", headers=headers)

    assert before.status_code == 200
    assert_error(after, 401, "UNAUTHORIZED")


@pytest.mark.asyncio
async def test_an_api_token_stops_working_when_its_user_is_deactivated(
    api: Api, db_session: AsyncSession, people: Actors, tokens
):
    before = await api.get("/auth/me", "stranger")
    await UserService(db_session).deactivate(people.admin, people.stranger.user_id)

    after = await api.get("/auth/me", "stranger")

    assert before.status_code == 200
    assert_error(after, 401, "UNAUTHORIZED")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "credential",
    [
        "alx_",
        "alx_unknown",
        "alx_" + "A" * 43,
        "alx_" + "A" * 4000,
        "ALX_" + "A" * 43,
        "alx-" + "A" * 43,
        "not.a.jwt",
        "x" * 5000,
    ],
)
async def test_credentials_that_were_never_issued_are_refused(api: Api, credential: str):
    response = await api.get("/auth/me", headers={"Authorization": f"Bearer {credential}"})

    assert_error(response, 401, "UNAUTHORIZED")


@pytest.mark.asyncio
async def test_a_credential_with_non_ascii_bytes_is_refused(api: Api):
    response = await api.client.get(
        "/api/v1/auth/me", headers=[(b"authorization", "Bearer \u00e9\u00e8".encode())]
    )

    assert_error(response, 401, "UNAUTHORIZED")


@pytest.mark.asyncio
async def test_a_jwt_cannot_be_replayed_as_an_api_token_prefix(api: Api, db_session: AsyncSession):
    await make_login_user(db_session)
    token = (await login(api)).json()["token"]

    response = await api.get("/auth/me", headers={"Authorization": f"Bearer alx_{token}"})

    assert_error(response, 401, "UNAUTHORIZED")


@pytest.mark.asyncio
async def test_with_private_reading_anonymous_callers_get_401_everywhere(
    api: Api, catalog: Catalog, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings.registry, "public_read", False)

    for path in (
        "/skills",
        "/skills/pub-skill",
        "/skills/pub-skill/versions",
        "/skills/pub-skill/versions/1.0.0",
        "/skills/pub-skill/versions/1.0.0/entrypoint",
        "/skills/pub-skill/versions/1.0.0/files",
        "/skills/pub-skill/versions/1.0.0/files/docs/guide.md",
        "/skills/pub-skill/versions/1.0.0/raw/docs/guide.md",
        "/skills/pub-skill/versions/1.0.0/download",
        "/tags",
        "/categories",
    ):
        response = await api.get(path)
        assert_error(response, 401, "UNAUTHORIZED")


@pytest.mark.asyncio
async def test_with_private_reading_any_valid_credential_reads(
    api: Api, catalog: Catalog, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings.registry, "public_read", False)

    for actor in ("stranger_read", "owner_read", "admin_read", "owner"):
        response = await api.get("/skills/pub-skill", actor)
        assert response.status_code == 200, actor


@pytest.mark.asyncio
async def test_with_private_reading_health_info_and_login_stay_public(
    api: Api, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    await make_login_user(db_session)
    monkeypatch.setattr(settings.registry, "public_read", False)

    health = await api.get("/health")
    info = await api.get("/info")
    logged_in = await login(api)

    assert health.status_code == 200
    assert info.status_code == 200
    assert info.json()["public_read"] is False
    assert logged_in.status_code == 200


@pytest.mark.asyncio
async def test_the_token_scope_reaches_the_service_through_the_dependency(api: Api):
    read = await api.post("/tokens", "owner_read", json={"name": "nope"})
    write = await api.post("/tokens", "owner", json={"name": "yep"})

    assert_error(read, 403, "FORBIDDEN")
    assert write.status_code == 201


@pytest.mark.asyncio
async def test_me_is_401_when_the_account_disappears_after_the_credential_was_checked(
    api: Api, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    await make_login_user(db_session)
    token = (await login(api)).json()["token"]

    async def nobody(db, user_id):
        return None

    monkeypatch.setattr(user_repo, "get_user", nobody)

    response = await api.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert_error(response, 401, "UNAUTHORIZED")
