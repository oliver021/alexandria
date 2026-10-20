from __future__ import annotations

import uuid

import pytest

from tests.test_api.support import (
    DRAFT_ENTRY,
    FOREIGN_ENTRY,
    HIDDEN_ENTRY,
    PUBLISHED_ENTRY,
    Api,
    Catalog,
    assert_error,
)

SUMMARY_FIELDS = {
    "id",
    "name",
    "display_name",
    "description",
    "kind",
    "category",
    "tags",
    "status",
    "latest_version",
    "owner_id",
    "created_at",
    "updated_at",
}
VERSION_FIELDS = {
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


def names(response) -> list[str]:
    return [item["name"] for item in response.json()["items"]]


@pytest.mark.asyncio
async def test_anonymous_callers_search_only_published_entries(api: Api, catalog: Catalog):
    response = await api.get("/skills")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"items", "total", "limit", "offset"}
    assert names(response) == [FOREIGN_ENTRY, PUBLISHED_ENTRY]
    assert body["total"] == 2
    assert (body["limit"], body["offset"]) == (20, 0)


@pytest.mark.asyncio
async def test_a_search_result_has_the_documented_shape(api: Api, catalog: Catalog):
    response = await api.get("/skills", params={"q": PUBLISHED_ENTRY})

    item = response.json()["items"][0]
    assert set(item) == SUMMARY_FIELDS
    assert item["name"] == PUBLISHED_ENTRY
    assert item["display_name"] == "Pub Skill"
    assert item["kind"] == "skill"
    assert item["status"] == "published"
    assert item["category"] == "ops"
    assert item["tags"] == ["alpha", "beta"]
    assert item["latest_version"] == "1.1.0"
    assert uuid.UUID(item["id"]) and uuid.UUID(item["owner_id"])
    assert item["created_at"].endswith("Z") or item["created_at"].endswith("+00:00")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("actor", "expected"),
    [
        ("anonymous", [FOREIGN_ENTRY, PUBLISHED_ENTRY]),
        ("stranger", [FOREIGN_ENTRY, PUBLISHED_ENTRY]),
        ("stranger_read", [FOREIGN_ENTRY, PUBLISHED_ENTRY]),
        ("owner", [DRAFT_ENTRY, FOREIGN_ENTRY, HIDDEN_ENTRY, PUBLISHED_ENTRY]),
        ("owner_read", [DRAFT_ENTRY, FOREIGN_ENTRY, HIDDEN_ENTRY, PUBLISHED_ENTRY]),
        ("admin", [DRAFT_ENTRY, FOREIGN_ENTRY, HIDDEN_ENTRY, PUBLISHED_ENTRY]),
        ("admin_read", [DRAFT_ENTRY, FOREIGN_ENTRY, HIDDEN_ENTRY, PUBLISHED_ENTRY]),
    ],
)
async def test_each_caller_searches_what_they_may_see(
    api: Api, catalog: Catalog, actor: str, expected: list[str]
):
    response = await api.get("/skills", actor)

    assert names(response) == expected
    assert response.json()["total"] == len(expected)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("actor", "status", "expected"),
    [
        ("owner", "draft", [DRAFT_ENTRY]),
        ("owner", "unpublished", [HIDDEN_ENTRY]),
        ("owner", "published", [FOREIGN_ENTRY, PUBLISHED_ENTRY]),
        ("admin", "unpublished", [HIDDEN_ENTRY]),
        ("stranger", "draft", []),
        ("stranger", "unpublished", []),
        ("anonymous", "draft", []),
        ("anonymous", "unpublished", []),
        ("anonymous", "published", [FOREIGN_ENTRY, PUBLISHED_ENTRY]),
    ],
)
async def test_the_status_filter_only_reveals_entries_the_caller_may_see(
    api: Api, catalog: Catalog, actor: str, status: str, expected: list[str]
):
    response = await api.get("/skills", actor, params={"status": status})

    assert response.status_code == 200
    assert names(response) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("actor", "expected"),
    [
        ("owner", [DRAFT_ENTRY, HIDDEN_ENTRY, PUBLISHED_ENTRY]),
        ("stranger", [FOREIGN_ENTRY]),
        ("admin", []),
    ],
)
async def test_mine_lists_the_entries_owned_by_the_caller(
    api: Api, catalog: Catalog, actor: str, expected: list[str]
):
    response = await api.get("/skills", actor, params={"mine": "true"})

    assert names(response) == expected


@pytest.mark.asyncio
async def test_mine_needs_a_credential(api: Api, catalog: Catalog):
    response = await api.get("/skills", params={"mine": "true"})

    assert_error(response, 401, "UNAUTHORIZED")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("actor", "kind", "expected"),
    [
        ("anonymous", "skill", [FOREIGN_ENTRY, PUBLISHED_ENTRY]),
        ("anonymous", "grounding", []),
        ("owner", "grounding", [HIDDEN_ENTRY]),
        ("owner", "skill", [DRAFT_ENTRY, FOREIGN_ENTRY, PUBLISHED_ENTRY]),
        ("owner", "GROUNDING", [HIDDEN_ENTRY]),
    ],
)
async def test_the_kind_filter(api: Api, catalog: Catalog, actor: str, kind: str, expected):
    response = await api.get("/skills", actor, params={"kind": kind})

    assert names(response) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"tag": "beta"}, [FOREIGN_ENTRY, PUBLISHED_ENTRY]),
        ({"tag": ["alpha", "beta"]}, [PUBLISHED_ENTRY]),
        ({"tag": ["beta", "gamma"]}, [FOREIGN_ENTRY]),
        ({"tag": ["alpha", "gamma"]}, []),
        ({"tag": "wip"}, []),
        ({"category": "ops"}, [PUBLISHED_ENTRY]),
        ({"category": "dev"}, [FOREIGN_ENTRY]),
        ({"category": "lab"}, []),
        ({"tag": "beta", "category": "ops"}, [PUBLISHED_ENTRY]),
    ],
)
async def test_tag_and_category_filters_combine_with_and(
    api: Api, catalog: Catalog, params, expected
):
    response = await api.get("/skills", params=params)

    assert names(response) == expected


@pytest.mark.asyncio
async def test_a_draft_entry_stays_out_of_the_tag_filter_for_strangers_but_not_owners(
    api: Api, catalog: Catalog
):
    anonymous = await api.get("/skills", params={"tag": "wip"})
    owner = await api.get("/skills", "owner", params={"tag": "wip"})

    assert names(anonymous) == []
    assert names(owner) == [DRAFT_ENTRY]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("pub", [PUBLISHED_ENTRY]),
        ("PUB-SKILL", [PUBLISHED_ENTRY]),
        ("skill body", [FOREIGN_ENTRY, PUBLISHED_ENTRY]),
        ("foreign body", [FOREIGN_ENTRY]),
        ("pub foreign", []),
        ("nothing-matches-this", []),
        ("alpha", [PUBLISHED_ENTRY]),
        ("%", []),
        ("_", []),
        ("ünï", []),
    ],
)
async def test_free_text_search_requires_every_word(api: Api, catalog: Catalog, query, expected):
    response = await api.get("/skills", params={"q": query})

    assert response.status_code == 200
    assert names(response) == expected


@pytest.mark.asyncio
async def test_the_best_name_match_comes_first_by_default_when_searching(
    api: Api, catalog: Catalog
):
    response = await api.get("/skills", params={"q": "skill"})

    assert names(response) == [FOREIGN_ENTRY, PUBLISHED_ENTRY]


@pytest.mark.asyncio
@pytest.mark.parametrize("sort", ["name", "updated", "created", "relevance", "NAME"])
async def test_every_documented_sort_order_is_accepted(api: Api, catalog: Catalog, sort: str):
    response = await api.get("/skills", params={"sort": sort})

    assert response.status_code == 200
    assert response.json()["total"] == 2


@pytest.mark.asyncio
async def test_sorting_by_name_is_alphabetical(api: Api, catalog: Catalog):
    response = await api.get("/skills", "owner", params={"sort": "name"})

    assert names(response) == sorted(names(response))


@pytest.mark.asyncio
async def test_pagination_slices_the_result_and_keeps_the_total(api: Api, catalog: Catalog):
    first = await api.get("/skills", "owner", params={"limit": 1, "offset": 0})
    second = await api.get("/skills", "owner", params={"limit": 1, "offset": 1})
    beyond = await api.get("/skills", "owner", params={"limit": 2, "offset": 10})

    assert names(first) == [DRAFT_ENTRY]
    assert names(second) == [FOREIGN_ENTRY]
    assert first.json()["total"] == second.json()["total"] == beyond.json()["total"] == 4
    assert (first.json()["limit"], second.json()["offset"]) == (1, 1)
    assert beyond.json()["items"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "fragment"),
    [
        ({"limit": 0}, "query.limit"),
        ({"limit": 101}, "query.limit"),
        ({"limit": -3}, "query.limit"),
        ({"limit": "many"}, "query.limit"),
        ({"offset": -1}, "query.offset"),
        ({"offset": "soon"}, "query.offset"),
        ({"offset": 2**31}, "query.offset"),
    ],
)
async def test_out_of_range_paging_is_a_validation_error_naming_the_field(
    api: Api, catalog: Catalog, params, fragment: str
):
    response = await api.get("/skills", params=params)

    error = assert_error(response, 422, "VALIDATION_ERROR")
    assert fragment in error["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "fragment"),
    [
        ({"kind": "plugin"}, "kind"),
        ({"status": "archived"}, "status"),
        ({"sort": "random"}, "sort"),
        ({"q": "x" * 513}, "q"),
        ({"q": "nul\x00byte"}, "q"),
        ({"category": "c" * 257}, "category"),
        ({"tag": "t" * 257}, "tag"),
    ],
)
async def test_invalid_filters_are_rejected_by_the_service_with_a_validation_error(
    api: Api, catalog: Catalog, params, fragment: str
):
    response = await api.get("/skills", params=params)

    error = assert_error(response, 422, "VALIDATION_ERROR")
    assert fragment in error["message"]


@pytest.mark.asyncio
async def test_a_search_with_many_filters_at_once_still_answers(api: Api, catalog: Catalog):
    response = await api.get(
        "/skills",
        "owner",
        params=[
            ("q", "skill"),
            ("kind", "skill"),
            ("tag", "alpha"),
            ("tag", "beta"),
            ("category", "ops"),
            ("status", "published"),
            ("sort", "name"),
            ("limit", "5"),
            ("offset", "0"),
        ],
    )

    assert names(response) == [PUBLISHED_ENTRY]


@pytest.mark.asyncio
async def test_the_detail_by_name_lists_the_visible_versions_newest_first(
    api: Api, catalog: Catalog
):
    response = await api.get(f"/skills/{PUBLISHED_ENTRY}")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == SUMMARY_FIELDS | {"versions"}
    assert [item["version"] for item in body["versions"]] == [
        "2.0.0-beta.1",
        "1.1.0",
        "1.0.0",
        "0.9.0",
    ]
    assert {item["status"] for item in body["versions"]} == {"published", "deprecated"}
    assert all(set(item) == VERSION_FIELDS for item in body["versions"])
    assert body["latest_version"] == "1.1.0"


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["owner", "owner_read", "admin", "admin_read"])
async def test_owners_and_admins_also_see_draft_versions(api: Api, catalog: Catalog, actor: str):
    response = await api.get(f"/skills/{PUBLISHED_ENTRY}", actor)

    versions = response.json()["versions"]
    assert [item["version"] for item in versions][:2] == ["2.0.0", "2.0.0-beta.1"]
    assert versions[0]["status"] == "draft"


@pytest.mark.asyncio
async def test_the_detail_by_uuid_is_the_detail_by_name(api: Api, catalog: Catalog):
    by_name = (await api.get(f"/skills/{PUBLISHED_ENTRY}")).json()

    by_id = await api.get(f"/skills/{by_name['id']}")

    assert by_id.status_code == 200
    assert by_id.json() == by_name


@pytest.mark.asyncio
async def test_the_detail_reports_each_version_with_its_hash_and_size(api: Api, catalog: Catalog):
    import hashlib

    body = (await api.get(f"/skills/{PUBLISHED_ENTRY}")).json()

    by_number = {item["version"]: item for item in body["versions"]}
    zip_bytes = catalog.zip_of(PUBLISHED_ENTRY, "1.0.0")
    assert by_number["1.0.0"]["sha256"] == hashlib.sha256(zip_bytes).hexdigest()
    assert by_number["1.0.0"]["size_bytes"] == len(zip_bytes)
    assert by_number["1.0.0"]["entrypoint"] == "SKILL.md"
    assert by_number["0.9.0"]["deprecated_at"] is not None
    assert by_number["1.0.0"]["deprecated_at"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", [DRAFT_ENTRY, HIDDEN_ENTRY])
@pytest.mark.parametrize("actor", ["anonymous", "stranger", "stranger_read"])
async def test_entries_that_are_not_visible_answer_404_not_403(
    api: Api, catalog: Catalog, entry: str, actor: str
):
    response = await api.get(f"/skills/{entry}", actor)

    assert_error(response, 404, "NOT_FOUND", reason=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", [DRAFT_ENTRY, HIDDEN_ENTRY])
@pytest.mark.parametrize("actor", ["owner", "owner_read", "admin", "admin_read"])
async def test_owners_and_admins_see_their_hidden_and_draft_entries(
    api: Api, catalog: Catalog, entry: str, actor: str
):
    response = await api.get(f"/skills/{entry}", actor)

    assert response.status_code == 200
    assert response.json()["name"] == entry


@pytest.mark.asyncio
async def test_a_hidden_entry_is_indistinguishable_from_a_missing_one(api: Api, catalog: Catalog):
    hidden = await api.get(f"/skills/{HIDDEN_ENTRY}")
    missing = await api.get("/skills/never-existed")

    hidden_error = assert_error(hidden, 404, "NOT_FOUND")
    missing_error = assert_error(missing, 404, "NOT_FOUND")
    assert hidden_error["message"].replace(HIDDEN_ENTRY, "X") == missing_error["message"].replace(
        "never-existed", "X"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ref",
    [
        "does-not-exist",
        str(uuid.uuid4()),
        "UPPER",
        "a" * 600,
        "%00",
        "%ff%fe",
        "..",
        "ünï",
        "a b",
        "0",
        "-",
    ],
)
async def test_unknown_references_are_404_never_500(api: Api, catalog: Catalog, ref: str):
    response = await api.get(f"/skills/{ref}")

    assert_error(response, 404, "NOT_FOUND")


@pytest.mark.asyncio
async def test_hiding_removes_an_entry_from_public_view_but_not_from_its_owner(
    api: Api, catalog: Catalog
):
    hidden = await api.delete(f"/skills/{PUBLISHED_ENTRY}", "owner")
    anonymous = await api.get(f"/skills/{PUBLISHED_ENTRY}")
    owner = await api.get(f"/skills/{PUBLISHED_ENTRY}", "owner")
    listing = await api.get("/skills")

    assert hidden.status_code == 204
    assert hidden.content == b""
    assert_error(anonymous, 404, "NOT_FOUND")
    assert owner.json()["status"] == "unpublished"
    assert names(listing) == [FOREIGN_ENTRY]


@pytest.mark.asyncio
async def test_hiding_twice_is_harmless(api: Api, catalog: Catalog):
    first = await api.delete(f"/skills/{PUBLISHED_ENTRY}", "owner")
    second = await api.delete(f"/skills/{PUBLISHED_ENTRY}", "owner")

    assert (first.status_code, second.status_code) == (204, 204)


@pytest.mark.asyncio
async def test_an_admin_can_hide_someone_elses_entry(api: Api, catalog: Catalog):
    response = await api.delete(f"/skills/{FOREIGN_ENTRY}", "admin")
    after = await api.get(f"/skills/{FOREIGN_ENTRY}")

    assert response.status_code == 204
    assert_error(after, 404, "NOT_FOUND")


@pytest.mark.asyncio
async def test_restoring_a_hidden_entry_with_a_published_version_publishes_it_again(
    api: Api, catalog: Catalog
):
    response = await api.post(f"/skills/{HIDDEN_ENTRY}/restore", "owner")
    anonymous = await api.get(f"/skills/{HIDDEN_ENTRY}")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == SUMMARY_FIELDS | {"versions"}
    assert body["status"] == "published"
    assert body["latest_version"] == "1.0.0"
    assert anonymous.status_code == 200


@pytest.mark.asyncio
async def test_restoring_an_entry_without_published_versions_leaves_it_a_draft(
    api: Api, catalog: Catalog
):
    await api.delete(f"/skills/{DRAFT_ENTRY}", "owner")

    response = await api.post(f"/skills/{DRAFT_ENTRY}/restore", "owner")
    anonymous = await api.get(f"/skills/{DRAFT_ENTRY}")

    assert response.status_code == 200
    assert response.json()["status"] == "draft"
    assert response.json()["latest_version"] is None
    assert_error(anonymous, 404, "NOT_FOUND")


@pytest.mark.asyncio
async def test_hide_and_restore_round_trip(api: Api, catalog: Catalog):
    before = (await api.get(f"/skills/{PUBLISHED_ENTRY}")).json()
    await api.delete(f"/skills/{PUBLISHED_ENTRY}", "owner")

    restored = await api.post(f"/skills/{PUBLISHED_ENTRY}/restore", "owner")

    assert restored.status_code == 200
    assert restored.json()["status"] == "published"
    assert restored.json()["latest_version"] == before["latest_version"]
    assert (await api.get(f"/skills/{PUBLISHED_ENTRY}")).status_code == 200


@pytest.mark.asyncio
async def test_the_old_catalog_routes_are_gone(api: Api, catalog: Catalog):
    created = await api.post("/skills", "owner", json={"name": "x", "display_name": "X"})
    updated = await api.client.put(
        f"/api/v1/skills/{PUBLISHED_ENTRY}",
        json={"display_name": "New"},
        headers=api.headers("owner"),
    )
    versions = await api.post(f"/skills/{PUBLISHED_ENTRY}/versions", "owner", json={})
    unpublish = await api.post(f"/skills/{PUBLISHED_ENTRY}/versions/1.0.0/unpublish", "owner")

    assert created.status_code == 405
    assert updated.status_code == 405
    assert versions.status_code == 405
    assert unpublish.status_code == 404
    for response in (created, updated, versions, unpublish):
        assert response.json()["error"]["code"] in {"METHOD_NOT_ALLOWED", "NOT_FOUND"}
