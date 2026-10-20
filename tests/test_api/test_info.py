from __future__ import annotations

import importlib.metadata

import pytest

from app.api import system
from app.config.settings import settings
from tests.test_api.support import PUBLIC_URL, Api, Catalog, assert_error

INFO_FIELDS = {"name", "version", "public_read", "public_url", "mcp_path", "limits"}
LIMIT_FIELDS = {
    "max_package_bytes",
    "max_uncompressed_bytes",
    "max_file_bytes",
    "max_files",
    "max_inline_read_bytes",
}


@pytest.mark.asyncio
async def test_info_describes_the_registry(api: Api):
    response = await api.get("/info")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == INFO_FIELDS
    assert body["name"] == "Alexandria"
    assert body["version"] == importlib.metadata.version("alexandria")
    assert body["public_read"] is True
    assert body["public_url"] == PUBLIC_URL
    assert body["mcp_path"] == "/mcp"
    assert set(body["limits"]) == LIMIT_FIELDS


@pytest.mark.asyncio
async def test_info_reports_the_configured_limits(api: Api, monkeypatch: pytest.MonkeyPatch):
    limits = settings.registry.limits
    monkeypatch.setattr(limits, "max_package_bytes", 1111)
    monkeypatch.setattr(limits, "max_uncompressed_bytes", 2222)
    monkeypatch.setattr(limits, "max_file_bytes", 3333)
    monkeypatch.setattr(limits, "max_files", 44)
    monkeypatch.setattr(limits, "max_inline_read_bytes", 555)

    body = (await api.get("/info")).json()

    assert body["limits"] == {
        "max_package_bytes": 1111,
        "max_uncompressed_bytes": 2222,
        "max_file_bytes": 3333,
        "max_files": 44,
        "max_inline_read_bytes": 555,
    }


@pytest.mark.asyncio
async def test_info_follows_the_read_mode_and_the_public_url(
    api: Api, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings.registry, "public_read", False)
    monkeypatch.setattr(settings.server, "public_url", "https://registry.other.test")

    body = (await api.get("/info")).json()

    assert body["public_read"] is False
    assert body["public_url"] == "https://registry.other.test"


@pytest.mark.asyncio
async def test_info_never_exposes_secrets_or_paths(api: Api):
    response = await api.get("/info")

    assert settings.auth.jwt_secret not in response.text
    assert "data/" not in response.text
    assert "sqlite" not in response.text.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["anonymous", "owner", "admin_read"])
async def test_info_is_the_same_for_everyone(api: Api, actor: str):
    anonymous = (await api.get("/info")).json()

    assert (await api.get("/info", actor)).json() == anonymous


def counts(response) -> dict[str, int]:
    return {item["name"]: item["count"] for item in response.json()}


@pytest.mark.asyncio
async def test_tags_count_the_published_entries_anonymous_callers_can_see(
    api: Api, catalog: Catalog
):
    response = await api.get("/tags")

    assert response.status_code == 200
    assert counts(response) == {"alpha": 1, "beta": 2, "gamma": 1}
    assert all(set(item) == {"name", "count"} for item in response.json())


@pytest.mark.asyncio
async def test_tags_are_ordered_by_use_then_name(api: Api, catalog: Catalog):
    items = (await api.get("/tags", "owner")).json()

    assert [(item["name"], item["count"]) for item in items] == [
        ("beta", 2),
        ("alpha", 1),
        ("archive", 1),
        ("gamma", 1),
        ("wip", 1),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["owner", "owner_read", "admin", "admin_read"])
async def test_owners_and_admins_also_count_their_drafts_and_hidden_entries(
    api: Api, catalog: Catalog, actor: str
):
    tags = await api.get("/tags", actor)
    categories = await api.get("/categories", actor)

    assert counts(tags) == {"alpha": 1, "beta": 2, "gamma": 1, "wip": 1, "archive": 1}
    assert counts(categories) == {"ops": 2, "dev": 1, "lab": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["anonymous", "stranger", "stranger_read"])
async def test_other_callers_never_see_hidden_taxonomy(api: Api, catalog: Catalog, actor: str):
    tags = await api.get("/tags", actor)
    categories = await api.get("/categories", actor)

    assert "wip" not in counts(tags) and "archive" not in counts(tags)
    assert counts(categories) == {"ops": 1, "dev": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "tags", "categories"),
    [
        ("grounding", {"archive": 1}, {"ops": 1}),
        ("skill", {"alpha": 1, "beta": 2, "gamma": 1, "wip": 1}, {"ops": 1, "dev": 1, "lab": 1}),
        ("SKILL", {"alpha": 1, "beta": 2, "gamma": 1, "wip": 1}, {"ops": 1, "dev": 1, "lab": 1}),
    ],
)
async def test_taxonomy_can_be_restricted_to_one_kind(
    api: Api, catalog: Catalog, kind: str, tags: dict, categories: dict
):
    assert counts(await api.get("/tags", "owner", params={"kind": kind})) == tags
    assert counts(await api.get("/categories", "owner", params={"kind": kind})) == categories


@pytest.mark.asyncio
async def test_taxonomy_limit_cuts_the_list(api: Api, catalog: Catalog):
    tags = await api.get("/tags", "owner", params={"limit": 2})
    categories = await api.get("/categories", "owner", params={"limit": 1})

    assert [item["name"] for item in tags.json()] == ["beta", "alpha"]
    assert [item["name"] for item in categories.json()] == ["ops"]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["/tags", "/categories"])
@pytest.mark.parametrize(
    ("params", "fragment"),
    [
        ({"limit": 0}, "query.limit"),
        ({"limit": 201}, "query.limit"),
        ({"limit": "lots"}, "query.limit"),
        ({"kind": "plugin"}, "kind"),
    ],
)
async def test_invalid_taxonomy_parameters_are_validation_errors(
    api: Api, catalog: Catalog, route: str, params, fragment: str
):
    response = await api.get(route, params=params)

    error = assert_error(response, 422, "VALIDATION_ERROR")
    assert fragment in error["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["/tags", "/categories"])
async def test_an_empty_catalog_has_empty_taxonomy(api: Api, route: str):
    response = await api.get(route)

    assert response.status_code == 200
    assert response.json() == []


def test_the_version_falls_back_when_the_distribution_is_not_installed(
    monkeypatch: pytest.MonkeyPatch,
):
    def missing(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(system.importlib.metadata, "version", missing)
    system.package_version.cache_clear()
    try:
        assert system.package_version() == "0.0.0"
    finally:
        system.package_version.cache_clear()
