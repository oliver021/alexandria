from __future__ import annotations

import hashlib

import pytest

from app.config.settings import settings
from tests.helpers.packages import (
    index_md,
    make_bomb_zip,
    make_encrypted_zip,
    make_grounding_zip,
    make_skill_zip,
    make_symlink_zip,
    make_traversal_zip,
    make_zip,
    skill_md,
)
from tests.test_api.support import (
    PUBLISHED_ENTRY,
    Api,
    Catalog,
    assert_error,
    assert_no_leak,
)

BOUNDARY = "alexandria-test-boundary"
MULTIPART = {"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"}


def multipart_body(*parts: tuple[str, str | bytes, dict[str, str]]) -> bytes:
    chunks: list[bytes] = []
    for name, content, extra in parts:
        disposition = f'form-data; name="{name}"'
        if "filename" in extra:
            disposition += f'; filename="{extra["filename"]}"'
        chunks.append(f"--{BOUNDARY}\r\nContent-Disposition: {disposition}\r\n\r\n".encode())
        chunks.append(content.encode() if isinstance(content, str) else content)
        chunks.append(b"\r\n")
    chunks.append(f"--{BOUNDARY}--\r\n".encode())
    return b"".join(chunks)


async def entry_count(api: Api) -> int:
    return (await api.get("/skills", "admin")).json()["total"]


@pytest.mark.asyncio
async def test_an_upload_creates_a_draft_entry_owned_by_the_uploader(api: Api, people, blob_store):
    data = make_skill_zip("fresh-skill", "1.0.0", tags=["x", "y"], category="ops")

    response = await api.upload(data, "owner")

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"skill", "version"}
    skill, version = body["skill"], body["version"]
    assert skill["name"] == "fresh-skill"
    assert skill["status"] == "draft"
    assert skill["latest_version"] is None
    assert skill["owner_id"] == people.owner.user_id
    assert skill["kind"] == "skill"
    assert skill["tags"] == ["x", "y"]
    assert skill["category"] == "ops"
    assert version["version"] == "1.0.0"
    assert version["status"] == "draft"
    assert version["skill_id"] == skill["id"]
    assert version["uploaded_by"] == people.owner.user_id
    assert version["published_by"] is None
    assert version["published_at"] is None
    assert version["sha256"] == hashlib.sha256(data).hexdigest()
    assert version["size_bytes"] == len(data)
    assert version["entrypoint"] == "SKILL.md"
    assert await blob_store.exists(version["sha256"])


@pytest.mark.asyncio
async def test_publish_true_publishes_the_version_and_the_entry(api: Api, people):
    response = await api.upload(make_skill_zip("live-skill", "1.0.0"), "owner", publish=True)

    body = response.json()
    assert response.status_code == 201
    assert body["version"]["status"] == "published"
    assert body["version"]["published_by"] == people.owner.user_id
    assert body["version"]["published_at"] is not None
    assert body["skill"]["status"] == "published"
    assert body["skill"]["latest_version"] == "1.0.0"
    assert (await api.get("/skills/live-skill")).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["true", "True", "1", "yes", "on"])
async def test_every_truthy_form_value_publishes(api: Api, value: str):
    response = await api.upload(make_skill_zip("truthy", "1.0.0"), "owner", publish=value)

    assert response.json()["version"]["status"] == "published"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["false", "False", "0", "no", "off"])
async def test_every_falsy_form_value_keeps_a_draft(api: Api, value: str):
    response = await api.upload(make_skill_zip("falsy", "1.0.0"), "owner", publish=value)

    assert response.json()["version"]["status"] == "draft"


@pytest.mark.asyncio
async def test_without_publish_the_registry_default_decides(
    api: Api, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings.registry, "auto_publish", True)

    default = await api.upload(make_skill_zip("auto-skill", "1.0.0"), "owner")
    explicit = await api.upload(make_skill_zip("manual-skill", "1.0.0"), "owner", publish=False)

    assert default.json()["version"]["status"] == "published"
    assert explicit.json()["version"]["status"] == "draft"


@pytest.mark.asyncio
async def test_blank_form_fields_count_as_absent(api: Api):
    response = await api.post(
        "/packages",
        "owner",
        files={"file": ("p.zip", make_skill_zip("blank-fields", "1.0.0"), "application/zip")},
        data={"version": "", "publish": ""},
    )

    assert response.status_code == 201
    assert response.json()["version"]["status"] == "draft"


@pytest.mark.asyncio
async def test_the_version_field_supplies_a_version_the_package_lacks(api: Api):
    response = await api.upload(make_skill_zip("unversioned", None), "owner", version="3.1.4")

    assert response.status_code == 201
    assert response.json()["version"]["version"] == "3.1.4"


@pytest.mark.asyncio
async def test_a_version_field_that_agrees_with_the_package_is_accepted(api: Api):
    response = await api.upload(
        make_skill_zip("agreeing", "2.0.0-rc.1"), "owner", version="2.0.0-rc.1"
    )

    assert response.status_code == 201


@pytest.mark.asyncio
async def test_a_version_field_that_disagrees_with_the_package_is_rejected(api: Api):
    response = await api.upload(make_skill_zip("disagreeing", "1.0.0"), "owner", version="1.0.1")

    assert_error(response, 422, "INVALID_PACKAGE", reason="VERSION_MISMATCH")
    assert (await api.get("/skills/disagreeing", "owner")).status_code == 404


@pytest.mark.asyncio
async def test_a_package_with_no_version_anywhere_is_rejected(api: Api):
    response = await api.upload(make_skill_zip("versionless", None), "owner")

    assert_error(response, 422, "INVALID_PACKAGE", reason="MISSING_FIELD")


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["1.0", "v1.0.0", "1.0.0+build", "one", "x" * 80])
async def test_an_invalid_version_field_is_a_validation_error(api: Api, version: str):
    response = await api.upload(make_skill_zip("badver", "1.0.0"), "owner", version=version)

    assert_error(response, 422, "VALIDATION_ERROR")


@pytest.mark.asyncio
async def test_the_owner_can_add_versions_to_an_entry(api: Api, catalog: Catalog, people):
    response = await api.upload(make_skill_zip(PUBLISHED_ENTRY, "3.0.0"), "owner", publish=True)

    assert response.status_code == 201
    assert response.json()["skill"]["owner_id"] == people.owner.user_id
    assert response.json()["skill"]["latest_version"] == "3.0.0"
    listing = await api.get(f"/skills/{PUBLISHED_ENTRY}/versions")
    assert listing.json()[0]["version"] == "3.0.0"


@pytest.mark.asyncio
async def test_an_admin_can_add_versions_to_someone_elses_entry_without_taking_it(
    api: Api, catalog: Catalog, people
):
    response = await api.upload(make_skill_zip(PUBLISHED_ENTRY, "3.0.0"), "admin")

    assert response.status_code == 201
    assert response.json()["skill"]["owner_id"] == people.owner.user_id
    assert response.json()["version"]["uploaded_by"] == people.admin.user_id


@pytest.mark.asyncio
async def test_another_user_cannot_add_versions_to_an_entry(api: Api, catalog: Catalog):
    response = await api.upload(make_skill_zip(PUBLISHED_ENTRY, "3.0.0"), "stranger")

    assert_error(response, 403, "FORBIDDEN")
    listing = await api.get(f"/skills/{PUBLISHED_ENTRY}/versions")
    assert "3.0.0" not in [item["version"] for item in listing.json()]


@pytest.mark.asyncio
async def test_a_version_cannot_be_uploaded_twice(api: Api, catalog: Catalog):
    response = await api.upload(make_skill_zip(PUBLISHED_ENTRY, "1.0.0"), "owner")

    assert_error(response, 409, "CONFLICT")


@pytest.mark.asyncio
async def test_an_entry_cannot_change_kind(api: Api, catalog: Catalog):
    response = await api.upload(make_grounding_zip(PUBLISHED_ENTRY, "9.0.0"), "owner")

    assert_error(response, 409, "CONFLICT")


@pytest.mark.asyncio
async def test_a_name_is_global_to_both_kinds(api: Api):
    await api.upload(make_grounding_zip("shared-name", "1.0.0"), "owner")

    response = await api.upload(make_skill_zip("shared-name", "1.0.1"), "owner")

    assert_error(response, 409, "CONFLICT")


@pytest.mark.asyncio
async def test_uploading_the_same_name_by_a_stranger_is_forbidden_not_a_conflict(
    api: Api, catalog: Catalog
):
    response = await api.upload(make_skill_zip(PUBLISHED_ENTRY, "1.0.0"), "stranger")

    assert_error(response, 403, "FORBIDDEN")


@pytest.mark.asyncio
async def test_the_extra_frontmatter_becomes_the_version_metadata(api: Api):
    data = make_skill_zip(
        "meta-skill",
        "1.0.0",
        extra_frontmatter={"license": "MIT", "authors": ["ada", "grace"], "weight": 3},
    )
    await api.upload(data, "owner", publish=True)

    response = await api.get("/skills/meta-skill/versions/1.0.0")

    assert response.json()["metadata"] == {
        "license": "MIT",
        "authors": ["ada", "grace"],
        "weight": 3,
    }


@pytest.mark.asyncio
async def test_a_wrapped_package_with_macos_junk_is_accepted_and_indexed_without_the_junk(
    api: Api,
):
    data = make_skill_zip(
        "mac-skill", "1.0.0", wrapper="mac-skill", macos_junk=True, files={"docs/a.md": "# a"}
    )

    response = await api.upload(data, "owner", publish=True)

    assert response.status_code == 201
    files = await api.get("/skills/mac-skill/versions/1.0.0/files")
    assert [item["path"] for item in files.json()["files"]] == ["SKILL.md", "docs/a.md"]


@pytest.mark.asyncio
async def test_a_grounding_package_is_deduced_from_its_index(api: Api):
    response = await api.upload(make_grounding_zip("ground-skill", "1.0.0"), "owner")

    assert response.json()["skill"]["kind"] == "grounding"
    assert response.json()["version"]["entrypoint"] == "INDEX.md"


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["owner", "admin", "stranger"])
async def test_every_user_with_write_scope_can_upload_a_new_entry(api: Api, actor: str):
    response = await api.upload(make_skill_zip(f"by-{actor}", "1.0.0"), actor)

    assert response.status_code == 201


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["owner_read", "stranger_read", "admin_read"])
async def test_a_read_token_cannot_upload(api: Api, actor: str):
    response = await api.upload(make_skill_zip("by-reader", "1.0.0"), actor)

    assert_error(response, 403, "FORBIDDEN")
    assert (await api.get("/skills/by-reader", "admin")).status_code == 404


@pytest.mark.asyncio
async def test_an_anonymous_caller_cannot_upload(api: Api):
    response = await api.upload(make_skill_zip("by-nobody", "1.0.0"), "anonymous")

    assert_error(response, 401, "UNAUTHORIZED")


@pytest.mark.asyncio
async def test_an_invalid_credential_cannot_upload(api: Api):
    response = await api.upload(
        make_skill_zip("by-fake", "1.0.0"), None, headers={"Authorization": "Bearer fake"}
    )

    assert_error(response, 401, "UNAUTHORIZED")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("actor", "status"), [("anonymous", 401), ("owner_read", 403), ("stranger_read", 403)]
)
async def test_authorization_is_decided_before_the_body_is_even_parsed(
    api: Api, actor: str, status: int
):
    garbage = {"Content-Type": "multipart/form-data; boundary=zz"}

    response = await api.post("/packages", actor, content=b"not multipart at all", headers=garbage)

    assert response.status_code == status


@pytest.mark.asyncio
async def test_an_unauthorized_upload_with_a_broken_content_type_is_still_401(api: Api):
    response = await api.post(
        "/packages", "anonymous", content=b"x", headers={"Content-Type": "multipart/form-data"}
    )

    assert_error(response, 401, "UNAUTHORIZED")


@pytest.mark.asyncio
async def test_a_body_without_the_file_part_is_a_validation_error(api: Api):
    response = await api.post("/packages", "owner", data={"version": "1.0.0"})

    error = assert_error(response, 422, "VALIDATION_ERROR")
    assert "file" in error["message"]


@pytest.mark.asyncio
async def test_a_multipart_body_whose_file_part_is_plain_text_is_a_validation_error(api: Api):
    body = multipart_body(("file", "just a string", {}))

    response = await api.post("/packages", "owner", content=body, headers=MULTIPART)

    assert_error(response, 422, "VALIDATION_ERROR")


@pytest.mark.asyncio
async def test_a_json_body_is_a_validation_error(api: Api):
    response = await api.post("/packages", "owner", json={"file": "x"})

    assert_error(response, 422, "VALIDATION_ERROR")


@pytest.mark.asyncio
async def test_an_empty_body_is_a_validation_error(api: Api):
    response = await api.post("/packages", "owner")

    assert_error(response, 422, "VALIDATION_ERROR")


@pytest.mark.asyncio
async def test_a_multipart_content_type_without_boundary_is_a_bad_request(api: Api):
    response = await api.post(
        "/packages", "owner", content=b"abc", headers={"Content-Type": "multipart/form-data"}
    )

    assert_error(response, 400, "BAD_REQUEST")


@pytest.mark.asyncio
async def test_a_malformed_multipart_body_is_a_bad_request(api: Api):
    response = await api.post(
        "/packages",
        "owner",
        content=b"--alexandria-test-boundary\r\nnot a header\r\n\r\ngarbage",
        headers=MULTIPART,
    )

    assert response.status_code == 400
    assert_error(response, 400, "BAD_REQUEST")


@pytest.mark.asyncio
async def test_two_file_parts_are_a_bad_request_and_nothing_is_stored(api: Api):
    one = make_skill_zip("twin-one", "1.0.0")
    two = make_skill_zip("twin-two", "1.0.0")
    body = multipart_body(
        ("file", one, {"filename": "one.zip"}), ("file", two, {"filename": "two.zip"})
    )

    response = await api.post("/packages", "owner", content=body, headers=MULTIPART)

    assert_error(response, 400, "BAD_REQUEST")
    assert await entry_count(api) == 0


@pytest.mark.asyncio
async def test_too_many_form_fields_are_a_bad_request(api: Api):
    parts = [(f"field{i}", "x", {}) for i in range(20)]
    parts.append(("file", make_skill_zip("fielded", "1.0.0"), {"filename": "p.zip"}))

    response = await api.post(
        "/packages", "owner", content=multipart_body(*parts), headers=MULTIPART
    )

    assert_error(response, 400, "BAD_REQUEST")


@pytest.mark.asyncio
async def test_unknown_form_fields_are_ignored(api: Api):
    body = multipart_body(
        ("comment", "hello", {}),
        ("file", make_skill_zip("commented", "1.0.0"), {"filename": "p.zip"}),
    )

    response = await api.post("/packages", "owner", content=body, headers=MULTIPART)

    assert response.status_code == 201


@pytest.mark.asyncio
async def test_a_text_field_sent_with_a_filename_counts_as_a_second_file(api: Api):
    body = multipart_body(
        ("version", "1.0.0", {"filename": "v.txt"}),
        ("file", make_skill_zip("filever", "1.0.0"), {"filename": "p.zip"}),
    )

    response = await api.post("/packages", "owner", content=body, headers=MULTIPART)

    assert_error(response, 400, "BAD_REQUEST")
    assert await entry_count(api) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["maybe", "2", "yes please", "tru"])
async def test_a_non_boolean_publish_flag_is_a_validation_error_naming_the_field(
    api: Api, value: str
):
    response = await api.upload(make_skill_zip("flagged", "1.0.0"), "owner", publish=value)

    error = assert_error(response, 422, "VALIDATION_ERROR")
    assert "body.publish" in error["message"]
    assert (await api.get("/skills/flagged", "owner")).status_code == 404


INVALID_PACKAGES = [
    ("not a zip", lambda: b"this is plain text", "NOT_A_ZIP"),
    ("empty file", lambda: b"", "NOT_A_ZIP"),
    ("no entrypoint", lambda: make_zip({"README.md": "# readme"}), "MISSING_ENTRYPOINT"),
    (
        "both entrypoints",
        lambda: make_zip({"SKILL.md": skill_md("both"), "INDEX.md": index_md("both")}),
        "AMBIGUOUS_ENTRYPOINT",
    ),
    ("no frontmatter", lambda: make_zip({"SKILL.md": "# just markdown\n"}), "BAD_FRONTMATTER"),
    ("invalid name", lambda: make_skill_zip("Not Valid", "1.0.0"), "INVALID_FIELD"),
    (
        "kind mismatch",
        lambda: make_skill_zip("kinds", "1.0.0", kind="grounding"),
        "KIND_MISMATCH",
    ),
    ("zip slip", lambda: make_traversal_zip("../evil.txt"), "UNSAFE_PATH"),
    ("absolute path", lambda: make_traversal_zip("/etc/passwd"), "UNSAFE_PATH"),
    ("symlink", lambda: make_symlink_zip(), "SYMLINK_NOT_ALLOWED"),
    ("encrypted", lambda: make_encrypted_zip(), "ENCRYPTED_ENTRY"),
    (
        "duplicate paths",
        lambda: make_zip([("SKILL.md", skill_md("dup")), ("a.txt", "1"), ("a.txt", "2")]),
        "DUPLICATE_PATH",
    ),
    (
        "yaml alias",
        lambda: make_zip(
            {
                "SKILL.md": "---\nname: aliased\nversion: 1.0.0\n"
                "description: &shared text\nnotes: *shared\n---\nbody\n"
            }
        ),
        "BAD_FRONTMATTER",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "build", "reason"), INVALID_PACKAGES, ids=[item[0] for item in INVALID_PACKAGES]
)
async def test_an_invalid_package_is_rejected_with_its_reason_and_leaves_no_trace(
    api: Api, blob_store, label: str, build, reason: str
):
    response = await api.upload(build(), "owner")

    error = assert_error(response, 422, "INVALID_PACKAGE", reason=reason)
    assert error["reason"] == reason
    assert_no_leak(response, str(blob_store.root))
    assert await entry_count(api) == 0
    assert list(blob_store.root.rglob("*.zip")) == []


@pytest.mark.asyncio
async def test_a_decompression_bomb_is_rejected_by_the_configured_limits(
    api: Api, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings.registry.limits, "max_uncompressed_bytes", 2 * 1024 * 1024)
    monkeypatch.setattr(settings.registry.limits, "max_file_bytes", 1024 * 1024)
    bomb = make_bomb_zip(8 * 1024 * 1024, declared_size=10)

    response = await api.upload(bomb, "owner")

    assert_error(response, 422, "INVALID_PACKAGE", reason="TOO_LARGE")


@pytest.mark.asyncio
async def test_a_package_with_more_files_than_allowed_is_rejected(
    api: Api, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings.registry.limits, "max_files", 2)
    data = make_skill_zip("many-files", "1.0.0", files={"a": "1", "b": "2", "c": "3"})

    response = await api.upload(data, "owner")

    assert_error(response, 422, "INVALID_PACKAGE", reason="TOO_MANY_FILES")


@pytest.mark.asyncio
async def test_the_invalid_package_message_does_not_echo_server_internals(api: Api, blob_store):
    response = await api.upload(make_traversal_zip("../../../../etc/passwd"), "owner")

    assert response.status_code == 422
    assert_no_leak(response, str(blob_store.root))


@pytest.mark.asyncio
async def test_what_was_uploaded_can_be_read_back_immediately(api: Api):
    data = make_skill_zip("roundtrip", "1.0.0", files={"docs/a.md": "# a\n"})
    await api.upload(data, "owner", publish=True)

    entrypoint = await api.get("/skills/roundtrip/versions/1.0.0/entrypoint")
    file = await api.get("/skills/roundtrip/versions/1.0.0/files/docs/a.md")
    download = await api.get("/skills/roundtrip/versions/latest/download")

    assert entrypoint.json()["name"] == "roundtrip"
    assert file.json()["content"] == "# a\n"
    assert download.content == data


@pytest.mark.asyncio
async def test_unauthorized_attempts_leave_the_catalog_untouched(api: Api, catalog: Catalog):
    before = await entry_count(api)

    for actor in ("anonymous", "owner_read", "stranger_read"):
        await api.upload(make_skill_zip("intruder", "1.0.0"), actor)

    assert await entry_count(api) == before
    assert (await api.get("/skills/intruder", "admin")).status_code == 404
