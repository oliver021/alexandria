from __future__ import annotations

import pytest

from tests.helpers.packages import make_skill_zip
from tests.test_api.support import (
    DRAFT_ENTRY,
    FOREIGN_ENTRY,
    HIDDEN_ENTRY,
    PUBLIC_URL,
    PUBLISHED_ENTRY,
    Api,
    Catalog,
    assert_error,
)

SUMMARY_FIELDS = {
    "id",
    "skill_id",
    "version",
    "status",
    "sha256",
    "size_bytes",
    "uncompressed_bytes",
    "file_count",
    "entrypoint",
    "display_name",
    "description",
    "category",
    "tags",
    "uploaded_by",
    "published_by",
    "published_at",
    "deprecated_at",
    "created_at",
}


def numbers(response) -> list[str]:
    return [item["version"] for item in response.json()]


async def upload(api: Api, name: str, version: str, *, publish: bool, actor: str = "owner", **kw):
    response = await api.upload(make_skill_zip(name, version, **kw), actor, publish=publish)
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.asyncio
async def test_listing_versions_goes_from_the_highest_to_the_lowest(api: Api, catalog: Catalog):
    response = await api.get(f"/skills/{PUBLISHED_ENTRY}/versions")

    assert response.status_code == 200
    assert numbers(response) == ["2.0.0-beta.1", "1.1.0", "1.0.0", "0.9.0"]
    assert all(set(item) == SUMMARY_FIELDS for item in response.json())


@pytest.mark.asyncio
async def test_listing_versions_hides_drafts_from_everyone_but_owners_and_admins(
    api: Api, catalog: Catalog
):
    anonymous = await api.get(f"/skills/{PUBLISHED_ENTRY}/versions")
    stranger = await api.get(f"/skills/{PUBLISHED_ENTRY}/versions", "stranger")
    owner = await api.get(f"/skills/{PUBLISHED_ENTRY}/versions", "owner")
    admin = await api.get(f"/skills/{PUBLISHED_ENTRY}/versions", "admin_read")

    assert "2.0.0" not in numbers(anonymous)
    assert "2.0.0" not in numbers(stranger)
    assert numbers(owner)[0] == "2.0.0"
    assert numbers(admin)[0] == "2.0.0"


@pytest.mark.asyncio
async def test_a_version_reports_who_uploaded_and_published_it(api: Api, catalog: Catalog, people):
    response = await api.get(f"/skills/{PUBLISHED_ENTRY}/versions/1.0.0")

    body = response.json()
    assert body["uploaded_by"] == people.owner.user_id
    assert body["published_by"] == people.owner.user_id
    assert body["published_at"] is not None
    assert body["status"] == "published"


@pytest.mark.asyncio
async def test_a_version_detail_adds_metadata_and_an_absolute_download_url(
    api: Api, catalog: Catalog
):
    response = await api.get(f"/skills/{PUBLISHED_ENTRY}/versions/1.0.0")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == SUMMARY_FIELDS | {"metadata", "download_url"}
    assert body["metadata"] == {}
    assert body["download_url"] == (
        f"{PUBLIC_URL}/api/v1/skills/{PUBLISHED_ENTRY}/versions/1.0.0/download"
    )
    assert body["version"] == "1.0.0"
    assert body["entrypoint"] == "SKILL.md"
    assert body["tags"] == ["alpha", "beta"]
    assert body["category"] == "ops"


@pytest.mark.asyncio
async def test_latest_resolves_to_the_highest_stable_published_version(api: Api, catalog: Catalog):
    response = await api.get(f"/skills/{PUBLISHED_ENTRY}/versions/latest")

    body = response.json()
    assert body["version"] == "1.1.0"
    assert body["download_url"].endswith("/versions/1.1.0/download")


@pytest.mark.asyncio
async def test_latest_never_picks_a_draft_or_a_deprecated_version(api: Api, catalog: Catalog):
    await api.post(f"/skills/{PUBLISHED_ENTRY}/versions/1.1.0/deprecate", "owner")

    response = await api.get(f"/skills/{PUBLISHED_ENTRY}/versions/latest", "owner")

    assert response.json()["version"] == "1.0.0"


@pytest.mark.asyncio
async def test_latest_falls_back_to_prereleases_when_nothing_stable_is_published(
    api: Api, catalog: Catalog
):
    await upload(api, "pre-only", "1.0.0-rc.1", publish=True)
    await upload(api, "pre-only", "1.0.0-rc.2", publish=True)
    await upload(api, "pre-only", "1.0.0-alpha", publish=True)

    response = await api.get("/skills/pre-only/versions/latest")

    assert response.json()["version"] == "1.0.0-rc.2"


@pytest.mark.asyncio
async def test_latest_follows_semver_not_upload_order_or_string_order(api: Api, catalog: Catalog):
    await upload(api, "ordered", "1.10.0", publish=True)
    await upload(api, "ordered", "1.2.0", publish=True)
    await upload(api, "ordered", "1.9.9", publish=True)

    listing = await api.get("/skills/ordered/versions")
    latest = await api.get("/skills/ordered/versions/latest")

    assert numbers(listing) == ["1.10.0", "1.9.9", "1.2.0"]
    assert latest.json()["version"] == "1.10.0"


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["anonymous", "owner", "admin"])
async def test_latest_of_an_entry_without_published_versions_is_404(
    api: Api, catalog: Catalog, actor: str
):
    response = await api.get(f"/skills/{DRAFT_ENTRY}/versions/latest", actor)

    assert_error(response, 404, "NOT_FOUND")


@pytest.mark.asyncio
async def test_a_deprecated_version_resolves_by_exact_number_for_everyone(
    api: Api, catalog: Catalog
):
    response = await api.get(f"/skills/{PUBLISHED_ENTRY}/versions/0.9.0")

    assert response.status_code == 200
    assert response.json()["status"] == "deprecated"
    assert response.json()["deprecated_at"] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("actor", "status"),
    [
        ("anonymous", 404),
        ("stranger", 404),
        ("stranger_read", 404),
        ("owner", 200),
        ("owner_read", 200),
        ("admin", 200),
        ("admin_read", 200),
    ],
)
async def test_draft_versions_are_visible_to_owners_and_admins_only(
    api: Api, catalog: Catalog, actor: str, status: int
):
    response = await api.get(f"/skills/{PUBLISHED_ENTRY}/versions/2.0.0", actor)

    assert response.status_code == status


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "version",
    [
        "9.9.9",
        "1.0",
        "v1.0.0",
        "1.0.0+build",
        "LATEST",
        "latest ",
        "1.0.0 ",
        "-",
        "%00",
        "x" * 80,
    ],
)
async def test_unknown_or_malformed_versions_are_404(api: Api, catalog: Catalog, version: str):
    response = await api.get(f"/skills/{PUBLISHED_ENTRY}/versions/{version}")

    assert_error(response, 404, "NOT_FOUND")


@pytest.mark.asyncio
async def test_versions_of_unknown_entries_are_404(api: Api, catalog: Catalog):
    listing = await api.get("/skills/ghost/versions")
    detail = await api.get("/skills/ghost/versions/1.0.0")
    latest = await api.get("/skills/ghost/versions/latest")

    for response in (listing, detail, latest):
        assert_error(response, 404, "NOT_FOUND")


@pytest.mark.asyncio
async def test_publishing_a_draft_makes_it_the_latest_and_visible(api: Api, catalog: Catalog):
    response = await api.post(f"/skills/{PUBLISHED_ENTRY}/versions/2.0.0/publish", "owner")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == SUMMARY_FIELDS
    assert body["status"] == "published"
    assert body["published_at"] is not None
    anonymous = await api.get(f"/skills/{PUBLISHED_ENTRY}/versions/latest")
    entry = await api.get(f"/skills/{PUBLISHED_ENTRY}")
    assert anonymous.json()["version"] == "2.0.0"
    assert entry.json()["latest_version"] == "2.0.0"


@pytest.mark.asyncio
async def test_publishing_the_first_version_publishes_the_entry(api: Api, catalog: Catalog):
    before = await api.get("/skills")
    response = await api.post(f"/skills/{DRAFT_ENTRY}/versions/0.1.0/publish", "owner")
    after = await api.get("/skills")

    assert response.status_code == 200
    assert DRAFT_ENTRY not in [item["name"] for item in before.json()["items"]]
    assert DRAFT_ENTRY in [item["name"] for item in after.json()["items"]]
    entry = await api.get(f"/skills/{DRAFT_ENTRY}")
    assert entry.json()["status"] == "published"
    assert entry.json()["latest_version"] == "0.1.0"


@pytest.mark.asyncio
async def test_publishing_a_deprecated_version_undoes_the_deprecation(api: Api, catalog: Catalog):
    response = await api.post(f"/skills/{PUBLISHED_ENTRY}/versions/0.9.0/publish", "owner")

    assert response.status_code == 200
    assert response.json()["status"] == "published"
    assert response.json()["deprecated_at"] is None


@pytest.mark.asyncio
async def test_publishing_twice_changes_nothing(api: Api, catalog: Catalog):
    first = await api.post(f"/skills/{PUBLISHED_ENTRY}/versions/1.0.0/publish", "owner")
    second = await api.post(f"/skills/{PUBLISHED_ENTRY}/versions/1.0.0/publish", "owner")

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()


@pytest.mark.asyncio
async def test_an_admin_can_publish_someone_elses_version(api: Api, catalog: Catalog):
    response = await api.post(f"/skills/{PUBLISHED_ENTRY}/versions/2.0.0/publish", "admin")

    assert response.status_code == 200
    assert response.json()["published_by"] != response.json()["uploaded_by"]


@pytest.mark.asyncio
async def test_deprecating_keeps_the_version_downloadable_but_not_latest(
    api: Api, catalog: Catalog
):
    response = await api.post(f"/skills/{PUBLISHED_ENTRY}/versions/1.1.0/deprecate", "owner")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "deprecated"
    assert body["deprecated_at"] is not None
    exact = await api.get(f"/skills/{PUBLISHED_ENTRY}/versions/1.1.0/download")
    entry = await api.get(f"/skills/{PUBLISHED_ENTRY}")
    assert exact.status_code == 200
    assert entry.json()["latest_version"] == "1.0.0"


@pytest.mark.asyncio
async def test_deprecating_twice_changes_nothing(api: Api, catalog: Catalog):
    first = await api.post(f"/skills/{PUBLISHED_ENTRY}/versions/1.0.0/deprecate", "owner")
    second = await api.post(f"/skills/{PUBLISHED_ENTRY}/versions/1.0.0/deprecate", "owner")

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entry", "version"),
    [(FOREIGN_ENTRY, "1.0.0"), (HIDDEN_ENTRY, "1.0.0")],
)
async def test_the_only_published_version_cannot_be_deprecated(
    api: Api, catalog: Catalog, entry: str, version: str
):
    actor = "stranger" if entry == FOREIGN_ENTRY else "owner"

    response = await api.post(f"/skills/{entry}/versions/{version}/deprecate", actor)

    error = assert_error(response, 409, "CONFLICT")
    assert "only" in error["message"].lower()
    entry_after = await api.get(f"/skills/{entry}", actor)
    assert entry_after.json()["latest_version"] == version


@pytest.mark.asyncio
async def test_a_draft_version_cannot_be_deprecated(api: Api, catalog: Catalog):
    response = await api.post(f"/skills/{PUBLISHED_ENTRY}/versions/2.0.0/deprecate", "owner")

    assert_error(response, 409, "CONFLICT")


@pytest.mark.asyncio
async def test_a_failed_deprecation_leaves_the_version_published(api: Api, catalog: Catalog):
    await api.post(f"/skills/{FOREIGN_ENTRY}/versions/1.0.0/deprecate", "stranger")

    after = await api.get(f"/skills/{FOREIGN_ENTRY}/versions/1.0.0")

    assert after.json()["status"] == "published"
    assert after.json()["deprecated_at"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["publish", "deprecate"])
@pytest.mark.parametrize("version", ["9.9.9", "latest", "1.0", "not-a-version"])
async def test_changing_the_state_of_an_unknown_version_is_404(
    api: Api, catalog: Catalog, action: str, version: str
):
    response = await api.post(f"/skills/{PUBLISHED_ENTRY}/versions/{version}/{action}", "owner")

    assert_error(response, 404, "NOT_FOUND")


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["publish", "deprecate"])
async def test_changing_the_state_of_an_unknown_entry_is_404(
    api: Api, catalog: Catalog, action: str
):
    response = await api.post(f"/skills/ghost/versions/1.0.0/{action}", "owner")

    assert_error(response, 404, "NOT_FOUND")


@pytest.mark.asyncio
async def test_publishing_and_deprecating_are_not_available_via_get(api: Api, catalog: Catalog):
    for action in ("publish", "deprecate"):
        response = await api.get(f"/skills/{PUBLISHED_ENTRY}/versions/1.0.0/{action}", "owner")
        assert_error(response, 405, "METHOD_NOT_ALLOWED")
        assert "POST" in response.headers["allow"]
