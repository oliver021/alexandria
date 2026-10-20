from __future__ import annotations

import uuid

import pytest

from app.auth.password import MAX_PASSWORD_LENGTH
from tests.test_api.support import Api, assert_error

USER_FIELDS = {"id", "username", "email", "role", "is_active", "created_at"}
PASSWORD = "long enough password"


def new_user(username: str = "dave", **overrides) -> dict:
    body = {"username": username, "email": f"{username}@example.com", "password": PASSWORD}
    body.update(overrides)
    return body


@pytest.mark.asyncio
async def test_an_admin_lists_every_account_without_password_data(api: Api):
    response = await api.get("/users", "admin")

    assert response.status_code == 200
    users = response.json()
    assert {user["username"] for user in users} == {"alice", "bob", "root"}
    assert all(set(user) == USER_FIELDS for user in users)
    assert all(isinstance(user["id"], str) and uuid.UUID(user["id"]) for user in users)
    assert {user["role"] for user in users} == {"publisher", "admin"}
    assert all(user["is_active"] is True for user in users)
    assert "password" not in response.text.lower()
    assert "hash" not in response.text.lower()


@pytest.mark.asyncio
async def test_an_admin_creates_a_publisher_by_default(api: Api):
    response = await api.post("/users", "admin", json=new_user())

    assert response.status_code == 201
    body = response.json()
    assert set(body) == USER_FIELDS
    assert body["username"] == "dave"
    assert body["email"] == "dave@example.com"
    assert body["role"] == "publisher"
    assert body["is_active"] is True


@pytest.mark.asyncio
async def test_an_admin_can_create_another_admin(api: Api):
    response = await api.post("/users", "admin", json=new_user("eve", role="admin"))

    assert response.status_code == 201
    assert response.json()["role"] == "admin"


@pytest.mark.asyncio
async def test_a_created_user_can_log_in_and_use_the_registry(api: Api):
    await api.post("/users", "admin", json=new_user())

    login = await api.post("/auth/login", json={"username": "dave", "password": PASSWORD})
    token = login.json()["token"]
    me = await api.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert login.status_code == 200
    assert me.json()["username"] == "dave"
    assert me.json()["role"] == "publisher"


@pytest.mark.asyncio
async def test_a_created_user_appears_in_the_list(api: Api):
    created = (await api.post("/users", "admin", json=new_user())).json()

    users = (await api.get("/users", "admin")).json()

    assert created["id"] in {user["id"] for user in users}


@pytest.mark.asyncio
async def test_a_duplicate_username_is_a_conflict(api: Api):
    first = await api.post("/users", "admin", json=new_user())
    second = await api.post("/users", "admin", json=new_user(email="other@example.com"))

    assert first.status_code == 201
    assert_error(second, 409, "CONFLICT")


@pytest.mark.asyncio
async def test_a_duplicate_email_is_a_conflict(api: Api):
    first = await api.post("/users", "admin", json=new_user())
    second = await api.post("/users", "admin", json=new_user("other", email="dave@example.com"))

    assert first.status_code == 201
    assert_error(second, 409, "CONFLICT")


@pytest.mark.asyncio
async def test_an_unknown_role_is_a_validation_error(api: Api):
    response = await api.post("/users", "admin", json=new_user(role="superuser"))

    error = assert_error(response, 422, "VALIDATION_ERROR")
    assert "role" in error["message"].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({}, "body.username"),
        ({"username": "x", "email": "x@example.com"}, "body.password"),
        ({"username": "", "email": "x@example.com", "password": PASSWORD}, "body.username"),
        ({"username": "x", "email": "", "password": PASSWORD}, "body.email"),
        ({"username": "x", "email": "x@example.com", "password": "short"}, "body.password"),
        (
            {
                "username": "x",
                "email": "x@example.com",
                "password": "p" * (MAX_PASSWORD_LENGTH + 1),
            },
            "body.password",
        ),
        ({"username": "x" * 256, "email": "x@example.com", "password": PASSWORD}, "body.username"),
    ],
)
async def test_an_invalid_user_names_the_offending_field(api: Api, body, field: str):
    response = await api.post("/users", "admin", json=body)

    error = assert_error(response, 422, "VALIDATION_ERROR")
    assert field in error["message"]


@pytest.mark.asyncio
async def test_a_password_at_the_maximum_length_is_accepted(api: Api):
    response = await api.post("/users", "admin", json=new_user(password="p" * MAX_PASSWORD_LENGTH))

    assert response.status_code == 201


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["username", "email", "password"])
async def test_a_nul_character_in_a_user_field_is_rejected(api: Api, field: str):
    body = new_user()
    body[field] = body[field] + "\x00"

    response = await api.post("/users", "admin", json=body)

    assert_error(response, 422, "VALIDATION_ERROR")


@pytest.mark.asyncio
async def test_a_failed_creation_leaves_the_account_list_alone(api: Api):
    await api.post("/users", "admin", json=new_user(role="nope"))
    await api.post("/users", "admin", json=new_user(password="x"))

    users = (await api.get("/users", "admin")).json()

    assert len(users) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["owner", "stranger", "owner_read", "stranger_read"])
async def test_a_publisher_cannot_manage_users(api: Api, actor: str):
    listing = await api.get("/users", actor)
    creation = await api.post("/users", actor, json=new_user())

    assert_error(listing, 403, "FORBIDDEN")
    assert_error(creation, 403, "FORBIDDEN")
    assert len((await api.get("/users", "admin")).json()) == 3


@pytest.mark.asyncio
async def test_an_admin_with_a_read_token_cannot_manage_users(api: Api):
    listing = await api.get("/users", "admin_read")
    creation = await api.post("/users", "admin_read", json=new_user())

    assert_error(listing, 403, "FORBIDDEN")
    assert_error(creation, 403, "FORBIDDEN")


@pytest.mark.asyncio
async def test_anonymous_callers_cannot_manage_users(api: Api):
    listing = await api.get("/users")
    creation = await api.post("/users", json=new_user())

    assert_error(listing, 401, "UNAUTHORIZED")
    assert_error(creation, 401, "UNAUTHORIZED")


@pytest.mark.asyncio
async def test_a_forbidden_creation_does_not_reveal_whether_the_name_is_taken(api: Api):
    taken = await api.post("/users", "owner", json=new_user("alice"))
    free = await api.post("/users", "owner", json=new_user("nobody-yet"))

    assert taken.status_code == free.status_code == 403
    assert taken.json() == free.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{}, {"username": "x"}, {"password": "short"}])
@pytest.mark.parametrize(
    ("actor", "status", "code"),
    [
        ("anonymous", 401, "UNAUTHORIZED"),
        ("admin_read", 403, "FORBIDDEN"),
        ("owner_read", 403, "FORBIDDEN"),
        ("admin", 422, "VALIDATION_ERROR"),
    ],
)
async def test_credentials_are_judged_before_the_user_request_is_validated(
    api: Api, body, actor: str, status: int, code: str
):
    response = await api.post("/users", actor, json=body)

    assert_error(response, status, code)
