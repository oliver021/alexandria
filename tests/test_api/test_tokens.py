from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.config.settings import settings
from tests.helpers.packages import make_skill_zip
from tests.test_api.support import Api, assert_error

TOKEN_FORMAT = re.compile(r"alx_[A-Za-z0-9_-]{43}")
TOKEN_FIELDS = {
    "id",
    "name",
    "prefix",
    "scope",
    "created_at",
    "expires_at",
    "last_used_at",
    "revoked_at",
}


async def create(api: Api, actor: str = "owner", **body):
    body.setdefault("name", "ci")
    return await api.post("/tokens", actor, json=body)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_creating_a_token_shows_its_secret_once(api: Api):
    response = await create(api, name="deploy bot", scope="write", expires_in_days=30)

    assert response.status_code == 201
    body = response.json()
    assert set(body) == TOKEN_FIELDS | {"token"}
    assert TOKEN_FORMAT.fullmatch(body["token"])
    assert body["prefix"] == body["token"][:12]
    assert body["name"] == "deploy bot"
    assert body["scope"] == "write"
    assert uuid.UUID(body["id"])
    assert body["revoked_at"] is None
    assert body["last_used_at"] is None
    expires = datetime.fromisoformat(body["expires_at"])
    assert timedelta(days=29) < expires - datetime.now(UTC) <= timedelta(days=30)


@pytest.mark.asyncio
async def test_a_new_token_defaults_to_read_scope_and_never_expires(api: Api):
    body = (await create(api)).json()

    assert body["scope"] == "read"
    assert body["expires_at"] is None


@pytest.mark.asyncio
async def test_the_response_that_carries_a_secret_is_never_cached(api: Api):
    response = await create(api)

    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_every_token_is_unique(api: Api):
    first = (await create(api, name="a")).json()
    second = (await create(api, name="b")).json()

    assert first["token"] != second["token"]
    assert first["id"] != second["id"]


@pytest.mark.asyncio
async def test_a_new_read_token_reads_but_cannot_write(api: Api):
    token = (await create(api, scope="read")).json()["token"]

    me = await api.get("/auth/me", headers=bearer(token))
    upload = await api.upload(make_skill_zip("by-new-reader", "1.0.0"), None, headers=bearer(token))
    mint = await api.post("/tokens", json={"name": "child"}, headers=bearer(token))

    assert me.status_code == 200
    assert_error(upload, 403, "FORBIDDEN")
    assert_error(mint, 403, "FORBIDDEN")


@pytest.mark.asyncio
async def test_a_new_write_token_can_upload_and_mint_tokens(api: Api):
    token = (await create(api, scope="write")).json()["token"]

    upload = await api.upload(make_skill_zip("by-new-writer", "1.0.0"), None, headers=bearer(token))
    mint = await api.post("/tokens", json={"name": "child"}, headers=bearer(token))

    assert upload.status_code == 201
    assert mint.status_code == 201


@pytest.mark.asyncio
async def test_a_token_acts_as_its_user_never_as_someone_else(api: Api, people):
    token = (await create(api, "stranger", scope="write")).json()["token"]

    me = await api.get("/auth/me", headers=bearer(token))
    users = await api.get("/users", headers=bearer(token))

    assert me.json()["username"] == "bob"
    assert_error(users, 403, "FORBIDDEN")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({}, "body.name"),
        ({"name": ""}, "body.name"),
        ({"name": "x" * 101}, "body.name"),
        ({"name": 5}, "body.name"),
        ({"name": "ok", "scope": "admin"}, "body.scope"),
        ({"name": "ok", "scope": "WRITE"}, "body.scope"),
        ({"name": "ok", "expires_in_days": 0}, "body.expires_in_days"),
        ({"name": "ok", "expires_in_days": -1}, "body.expires_in_days"),
        ({"name": "ok", "expires_in_days": 3651}, "body.expires_in_days"),
        ({"name": "ok", "expires_in_days": "soon"}, "body.expires_in_days"),
    ],
)
async def test_an_invalid_token_request_names_the_offending_field(api: Api, body, field: str):
    response = await api.post("/tokens", "owner", json=body)

    error = assert_error(response, 422, "VALIDATION_ERROR")
    assert field in error["message"]


@pytest.mark.asyncio
async def test_a_blank_token_name_is_rejected_by_the_service(api: Api):
    response = await create(api, name="   ")

    assert_error(response, 422, "VALIDATION_ERROR")


@pytest.mark.asyncio
async def test_a_token_name_is_stored_trimmed(api: Api):
    body = (await create(api, name="  padded  ")).json()

    assert body["name"] == "padded"


@pytest.mark.asyncio
async def test_a_malformed_token_request_body_is_a_validation_error(api: Api):
    response = await api.post(
        "/tokens", "owner", content=b"[1,2", headers={"Content-Type": "application/json"}
    )

    assert_error(response, 422, "VALIDATION_ERROR")


@pytest.mark.asyncio
async def test_a_token_request_without_a_json_content_type_is_a_validation_error(api: Api):
    response = await api.post(
        "/tokens", "owner", content=b'{"name": "x"}', headers={"Content-Type": "text/plain"}
    )

    assert_error(response, 422, "VALIDATION_ERROR")


@pytest.mark.asyncio
async def test_unknown_fields_in_the_token_request_are_ignored(api: Api):
    response = await create(api, name="ok", user_id="someone-else", token="alx_chosen")

    assert response.status_code == 201
    assert response.json()["token"] != "alx_chosen"


@pytest.mark.asyncio
async def test_listing_shows_only_the_callers_tokens_without_secrets(api: Api):
    await create(api, name="mine")
    await create(api, "stranger", name="theirs")

    response = await api.get("/tokens", "owner")

    assert response.status_code == 200
    items = response.json()
    assert {item["name"] for item in items} == {"owner", "owner_read", "mine"}
    assert all(set(item) == TOKEN_FIELDS for item in items)
    assert "theirs" not in response.text
    assert not re.search(r"[0-9a-f]{64}", response.text)


@pytest.mark.asyncio
async def test_listing_a_secret_never_reveals_more_than_the_prefix(api: Api):
    created = (await create(api, name="secretive")).json()

    listed = (await api.get("/tokens", "owner")).json()

    assert created["token"] not in str(listed)
    item = next(entry for entry in listed if entry["id"] == created["id"])
    assert item["prefix"] == created["token"][:12]


@pytest.mark.asyncio
async def test_using_a_token_records_when_it_was_last_used(api: Api):
    created = (await create(api, name="tracked")).json()
    await api.get("/auth/me", headers=bearer(created["token"]))

    listed = (await api.get("/tokens", "owner")).json()

    item = next(entry for entry in listed if entry["id"] == created["id"])
    assert item["last_used_at"] is not None


@pytest.mark.asyncio
async def test_revoking_a_token_makes_it_useless_immediately(api: Api):
    created = (await create(api, name="doomed", scope="write")).json()
    before = await api.get("/auth/me", headers=bearer(created["token"]))

    revoked = await api.delete(f"/tokens/{created['id']}", "owner")
    after = await api.get("/auth/me", headers=bearer(created["token"]))

    assert before.status_code == 200
    assert revoked.status_code == 204
    assert revoked.content == b""
    assert_error(after, 401, "UNAUTHORIZED")


@pytest.mark.asyncio
async def test_a_revoked_token_stays_in_the_listing_marked_as_revoked(api: Api):
    created = (await create(api, name="doomed")).json()
    await api.delete(f"/tokens/{created['id']}", "owner")

    listed = (await api.get("/tokens", "owner")).json()

    item = next(entry for entry in listed if entry["id"] == created["id"])
    assert item["revoked_at"] is not None


@pytest.mark.asyncio
async def test_revoking_twice_is_harmless(api: Api):
    created = (await create(api)).json()

    first = await api.delete(f"/tokens/{created['id']}", "owner")
    second = await api.delete(f"/tokens/{created['id']}", "owner")

    assert (first.status_code, second.status_code) == (204, 204)


@pytest.mark.asyncio
async def test_a_token_can_revoke_itself(api: Api):
    created = (await create(api, scope="write")).json()

    revoked = await api.delete(f"/tokens/{created['id']}", headers=bearer(created["token"]))
    after = await api.get("/auth/me", headers=bearer(created["token"]))

    assert revoked.status_code == 204
    assert_error(after, 401, "UNAUTHORIZED")


@pytest.mark.asyncio
async def test_a_user_cannot_revoke_someone_elses_token(api: Api):
    theirs = (await create(api, "stranger", name="theirs")).json()

    response = await api.delete(f"/tokens/{theirs['id']}", "owner")

    assert_error(response, 404, "NOT_FOUND")
    still_works = await api.get("/auth/me", headers=bearer(theirs["token"]))
    assert still_works.status_code == 200


@pytest.mark.asyncio
async def test_an_admin_can_revoke_any_token(api: Api):
    theirs = (await create(api, "stranger", name="theirs")).json()

    response = await api.delete(f"/tokens/{theirs['id']}", "admin")
    after = await api.get("/auth/me", headers=bearer(theirs["token"]))

    assert response.status_code == 204
    assert_error(after, 401, "UNAUTHORIZED")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "token_id", [str(uuid.uuid4()), "not-an-id", "0", "x" * 300, "%00", "..%2Ftokens", "ünï"]
)
async def test_revoking_an_unknown_token_is_404(api: Api, token_id: str):
    response = await api.delete(f"/tokens/{token_id}", "owner")

    assert_error(response, 404, "NOT_FOUND")


@pytest.mark.asyncio
async def test_the_token_limit_per_user_is_enforced_and_revoking_frees_a_slot(
    api: Api, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings.tokens, "max_per_user", 4)
    first = await create(api, name="third")
    second = await create(api, name="fourth")
    over = await create(api, name="fifth")

    assert (first.status_code, second.status_code) == (201, 201)
    assert_error(over, 409, "CONFLICT")
    await api.delete(f"/tokens/{first.json()['id']}", "owner")
    freed = await create(api, name="again")
    assert freed.status_code == 201


@pytest.mark.asyncio
async def test_the_token_limit_is_per_user(api: Api, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings.tokens, "max_per_user", 3)
    await create(api, name="fills-owner")

    other = await create(api, "admin", name="admin-own")

    assert other.status_code == 201


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["POST", "GET"])
async def test_token_routes_need_a_credential(api: Api, method: str):
    response = await api.request(method, "/tokens", "anonymous", json={"name": "x"})

    assert_error(response, 401, "UNAUTHORIZED")


@pytest.mark.asyncio
async def test_revoking_needs_a_credential(api: Api):
    response = await api.delete(f"/tokens/{uuid.uuid4()}")

    assert_error(response, 401, "UNAUTHORIZED")


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["owner_read", "stranger_read", "admin_read"])
async def test_read_scope_cannot_manage_tokens(api: Api, actor: str):
    own = (await create(api, actor.removesuffix("_read"), name="own")).json()

    listing = await api.get("/tokens", actor)
    creation = await api.post("/tokens", actor, json={"name": "x"})
    revocation = await api.delete(f"/tokens/{own['id']}", actor)

    for response in (listing, creation, revocation):
        assert_error(response, 403, "FORBIDDEN")
    still_works = await api.get("/auth/me", headers=bearer(own["token"]))
    assert still_works.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{}, {"name": ""}, {"scope": "nonsense"}, {"name": 5}])
@pytest.mark.parametrize(
    ("actor", "status", "code"),
    [
        ("anonymous", 401, "UNAUTHORIZED"),
        ("owner_read", 403, "FORBIDDEN"),
        ("owner", 422, "VALIDATION_ERROR"),
    ],
)
async def test_credentials_are_judged_before_the_token_request_is_validated(
    api: Api, body, actor: str, status: int, code: str
):
    response = await api.post("/tokens", actor, json=body)

    assert_error(response, status, code)
