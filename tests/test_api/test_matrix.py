from __future__ import annotations

import uuid

import pytest

from app.config.settings import settings
from app.main import app
from tests.helpers.factories import (
    ACTOR_NAMES,
    MANAGERS,
    SEEDED_VERSIONS,
    VISIBLE_TO,
    write_outcome,
)
from tests.helpers.packages import make_grounding_zip, make_skill_zip
from tests.test_api.support import (
    DRAFT_ENTRY,
    FOREIGN_ENTRY,
    HIDDEN_ENTRY,
    OUTCOME_STATUS,
    PUBLISHED_ENTRY,
    Api,
    Catalog,
    assert_error,
)

ENTRIES = [PUBLISHED_ENTRY, DRAFT_ENTRY, HIDDEN_ENTRY, FOREIGN_ENTRY]
READ_ROUTES = [
    "/skills/{name}",
    "/skills/{name}/versions",
    "/skills/{name}/versions/{version}",
    "/skills/{name}/versions/{version}/entrypoint",
    "/skills/{name}/versions/{version}/files",
    "/skills/{name}/versions/{version}/files/docs/guide.md",
    "/skills/{name}/versions/{version}/raw/docs/guide.md",
    "/skills/{name}/versions/{version}/download",
]
LATEST_ROUTES = [
    "/skills/{name}/versions/latest",
    "/skills/{name}/versions/latest/entrypoint",
    "/skills/{name}/versions/latest/files",
    "/skills/{name}/versions/latest/files/docs/guide.md",
    "/skills/{name}/versions/latest/raw/docs/guide.md",
    "/skills/{name}/versions/latest/download",
]
DEPRECATE_SUCCESS = {PUBLISHED_ENTRY: 200}
LATEST_VISIBLE = {
    PUBLISHED_ENTRY: set(ACTOR_NAMES),
    FOREIGN_ENTRY: set(ACTOR_NAMES),
    HIDDEN_ENTRY: set(VISIBLE_TO[HIDDEN_ENTRY]),
    DRAFT_ENTRY: set(),
}


def path_of(template: str, entry: str) -> str:
    return template.format(name=entry, version=SEEDED_VERSIONS[entry])


async def statuses(api: Api, method: str, path: str, **kwargs) -> dict[str, int]:
    return {
        actor: (await api.request(method, path, actor, **kwargs)).status_code
        for actor in ACTOR_NAMES
    }


def management_status(actor: str, entry: str, success: int, *, conflict: bool = False) -> int:
    outcome = write_outcome(actor, entry)
    if outcome is not None:
        return OUTCOME_STATUS[outcome]
    return 409 if conflict else success


async def run_management_matrix(
    api: Api, method: str, path: str, entry: str, success: int, *, conflict: bool = False
) -> None:
    expected = {
        actor: management_status(actor, entry, success, conflict=conflict) for actor in ACTOR_NAMES
    }
    order = sorted(ACTOR_NAMES, key=lambda actor: expected[actor] == success)
    observed = {actor: (await api.request(method, path, actor)).status_code for actor in order}
    assert observed == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ENTRIES)
@pytest.mark.parametrize("template", READ_ROUTES)
async def test_read_routes_only_show_each_entry_to_those_who_may_see_it(
    api: Api, mini_catalog: Catalog, template: str, entry: str
):
    observed = await statuses(api, "GET", path_of(template, entry))

    expected = {actor: 200 if actor in VISIBLE_TO[entry] else 404 for actor in ACTOR_NAMES}
    assert observed == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ENTRIES)
@pytest.mark.parametrize("template", LATEST_ROUTES)
async def test_latest_routes_follow_the_same_visibility_and_need_a_published_version(
    api: Api, mini_catalog: Catalog, template: str, entry: str
):
    observed = await statuses(api, "GET", path_of(template, entry))

    expected = {actor: 200 if actor in LATEST_VISIBLE[entry] else 404 for actor in ACTOR_NAMES}
    assert observed == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ENTRIES)
@pytest.mark.parametrize("template", READ_ROUTES + LATEST_ROUTES)
async def test_with_private_reading_anonymous_gets_401_and_everyone_else_the_same_answer(
    api: Api, mini_catalog: Catalog, monkeypatch: pytest.MonkeyPatch, template: str, entry: str
):
    path = path_of(template, entry)
    public = await statuses(api, "GET", path)
    monkeypatch.setattr(settings.registry, "public_read", False)

    private = await statuses(api, "GET", path)

    assert private["anonymous"] == 401
    assert {a: s for a, s in private.items() if a != "anonymous"} == {
        a: s for a, s in public.items() if a != "anonymous"
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ENTRIES)
@pytest.mark.parametrize("template", READ_ROUTES + LATEST_ROUTES)
async def test_an_invalid_credential_is_401_whoever_could_read(
    api: Api, mini_catalog: Catalog, template: str, entry: str
):
    response = await api.get(
        path_of(template, entry), headers={"Authorization": "Bearer not-valid"}
    )

    assert_error(response, 401, "UNAUTHORIZED")


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ENTRIES)
async def test_hiding_follows_ownership_and_scope(api: Api, mini_catalog: Catalog, entry: str):
    await run_management_matrix(api, "DELETE", f"/skills/{entry}", entry, 204)


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ENTRIES)
async def test_restoring_follows_ownership_and_scope(api: Api, mini_catalog: Catalog, entry: str):
    await run_management_matrix(api, "POST", f"/skills/{entry}/restore", entry, 200)


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ENTRIES)
async def test_publishing_follows_ownership_and_scope(api: Api, mini_catalog: Catalog, entry: str):
    path = path_of("/skills/{name}/versions/{version}/publish", entry)

    await run_management_matrix(api, "POST", path, entry, 200)


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ENTRIES)
async def test_deprecating_follows_ownership_and_scope_and_the_last_published_version_rule(
    api: Api, mini_catalog: Catalog, entry: str
):
    path = path_of("/skills/{name}/versions/{version}/deprecate", entry)

    await run_management_matrix(
        api, "POST", path, entry, 200, conflict=entry not in DEPRECATE_SUCCESS
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ENTRIES)
async def test_uploading_a_version_follows_ownership_and_scope(
    api: Api, mini_catalog: Catalog, entry: str
):
    expected: dict[str, int] = {}
    for actor in ACTOR_NAMES:
        if actor == "anonymous":
            expected[actor] = 401
        elif actor.endswith("_read"):
            expected[actor] = 403
        elif actor in MANAGERS[entry]:
            expected[actor] = 201
        else:
            expected[actor] = 403
    order = sorted(ACTOR_NAMES, key=lambda actor: expected[actor] == 201)

    maker = make_grounding_zip if entry == HIDDEN_ENTRY else make_skill_zip
    observed: dict[str, int] = {}
    for index, actor in enumerate(order):
        observed[actor] = (await api.upload(maker(entry, f"7.{index}.0"), actor)).status_code

    assert observed == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(("actor", "status"), [("stranger", 404), ("stranger_read", 403)])
async def test_a_stranger_cannot_use_management_routes_to_probe_hidden_entries(
    api: Api, mini_catalog: Catalog, actor: str, status: int
):
    hidden = await api.delete(f"/skills/{DRAFT_ENTRY}", actor)
    missing = await api.delete("/skills/no-such-entry", actor)

    assert hidden.status_code == missing.status_code == status
    assert hidden.json()["error"]["code"] == missing.json()["error"]["code"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "template"),
    [
        ("DELETE", "/skills/{name}"),
        ("POST", "/skills/{name}/restore"),
        ("POST", "/skills/{name}/versions/{version}/publish"),
        ("POST", "/skills/{name}/versions/{version}/deprecate"),
    ],
)
async def test_an_invalid_credential_cannot_manage_anything(
    api: Api, mini_catalog: Catalog, method: str, template: str
):
    response = await api.request(
        method,
        path_of(template, PUBLISHED_ENTRY),
        headers={"Authorization": "Bearer not-valid"},
    )

    assert_error(response, 401, "UNAUTHORIZED")
    assert (await api.get(f"/skills/{PUBLISHED_ENTRY}")).status_code == 200


PUBLIC = "public"
READ = "read"
AUTHENTICATED = "authenticated"
WRITE = "write"

ROUTE_CLASSES = {
    ("GET", "/health"): PUBLIC,
    ("GET", "/info"): PUBLIC,
    ("POST", "/auth/login"): PUBLIC,
    ("GET", "/auth/me"): AUTHENTICATED,
    ("GET", "/skills"): READ,
    ("GET", "/skills/{ref}"): READ,
    ("DELETE", "/skills/{ref}"): WRITE,
    ("POST", "/skills/{ref}/restore"): WRITE,
    ("POST", "/packages"): WRITE,
    ("GET", "/skills/{ref}/versions"): READ,
    ("GET", "/skills/{ref}/versions/{version}"): READ,
    ("POST", "/skills/{ref}/versions/{version}/publish"): WRITE,
    ("POST", "/skills/{ref}/versions/{version}/deprecate"): WRITE,
    ("GET", "/skills/{ref}/versions/{version}/entrypoint"): READ,
    ("GET", "/skills/{ref}/versions/{version}/files"): READ,
    ("GET", "/skills/{ref}/versions/{version}/files/{path}"): READ,
    ("GET", "/skills/{ref}/versions/{version}/raw/{path}"): READ,
    ("GET", "/skills/{ref}/versions/{version}/download"): READ,
    ("GET", "/tags"): READ,
    ("GET", "/categories"): READ,
    ("POST", "/tokens"): WRITE,
    ("GET", "/tokens"): WRITE,
    ("DELETE", "/tokens/{id}"): WRITE,
    ("GET", "/users"): WRITE,
    ("POST", "/users"): WRITE,
}


def documented_routes() -> set[tuple[str, str]]:
    return {
        (method.upper(), path.removeprefix("/api/v1"))
        for path, item in app.openapi()["paths"].items()
        if path.startswith("/api/v1")
        for method in item
    }


def concrete(template: str) -> str:
    return (
        template.replace("{ref}", PUBLISHED_ENTRY)
        .replace("{version}", "1.0.0")
        .replace("{path}", "docs/guide.md")
        .replace("{id}", str(uuid.uuid4()))
    )


def test_every_documented_route_is_classified_here():
    assert set(ROUTE_CLASSES) == documented_routes()


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "template"), sorted(ROUTE_CLASSES))
async def test_every_route_enforces_its_class_for_anonymous_and_read_only_callers(
    api: Api, mini_catalog: Catalog, method: str, template: str
):
    kind = ROUTE_CLASSES[(method, template)]
    path = concrete(template)
    extra = {"json": {}} if method == "POST" else {}

    anonymous = await api.request(method, path, "anonymous", **extra)
    reader = await api.request(method, path, "owner_read", **extra)

    if kind == PUBLIC:
        assert anonymous.status_code != 401 or template == "/auth/login"
    elif kind == READ:
        assert anonymous.status_code in (200, 404)
        assert reader.status_code in (200, 404)
    elif kind == AUTHENTICATED:
        assert_error(anonymous, 401, "UNAUTHORIZED")
        assert reader.status_code == 200
    else:
        assert_error(anonymous, 401, "UNAUTHORIZED")
        assert_error(reader, 403, "FORBIDDEN")


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "template"), sorted(ROUTE_CLASSES))
async def test_every_route_but_the_public_ones_refuses_anonymous_callers_in_private_mode(
    api: Api,
    mini_catalog: Catalog,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    template: str,
):
    monkeypatch.setattr(settings.registry, "public_read", False)
    path = concrete(template)
    extra = {"json": {}} if method == "POST" else {}

    response = await api.request(method, path, "anonymous", **extra)

    if ROUTE_CLASSES[(method, template)] == PUBLIC:
        assert response.status_code != 401 or template == "/auth/login"
    else:
        assert_error(response, 401, "UNAUTHORIZED")


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "template"), sorted(ROUTE_CLASSES))
async def test_every_route_but_the_public_ones_refuses_a_bogus_credential(
    api: Api, mini_catalog: Catalog, method: str, template: str
):
    path = concrete(template)
    extra = {"json": {}} if method == "POST" else {}

    response = await api.request(
        method, path, "anonymous", headers={"Authorization": "Bearer bogus"}, **extra
    )

    if ROUTE_CLASSES[(method, template)] == PUBLIC:
        assert response.status_code != 401 or template == "/auth/login"
    else:
        assert_error(response, 401, "UNAUTHORIZED")
