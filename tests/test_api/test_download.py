from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import random
import zipfile
from pathlib import Path

import pytest

from app.config.settings import settings
from tests.helpers.packages import make_skill_zip
from tests.test_api.support import (
    DRAFT_ENTRY,
    HIDDEN_ENTRY,
    PUBLIC_URL,
    PUBLISHED_ENTRY,
    Api,
    Catalog,
    assert_error,
    assert_no_leak,
    stream_through_the_app,
)

DOWNLOAD = f"/skills/{PUBLISHED_ENTRY}/versions/1.0.0/download"
LATEST_DOWNLOAD = f"/skills/{PUBLISHED_ENTRY}/versions/latest/download"
PUBLIC_CACHE = "public, immutable"
REVALIDATE_CACHE = "public, no-cache"
PRIVATE_CACHE = "private, no-store"


def etag_of(data: bytes) -> str:
    return f'"{hashlib.sha256(data).hexdigest()}"'


@pytest.mark.asyncio
async def test_a_public_download_is_the_uploaded_zip_with_the_contract_headers(
    api: Api, catalog: Catalog
):
    original = catalog.zip_of(PUBLISHED_ENTRY, "1.0.0")

    response = await api.get(DOWNLOAD)

    assert response.status_code == 200
    assert response.content == original
    headers = response.headers
    assert headers["content-type"] == "application/zip"
    assert headers["content-length"] == str(len(original))
    assert headers["content-disposition"] == f'attachment; filename="{PUBLISHED_ENTRY}-1.0.0.zip"'
    assert headers["x-content-sha256"] == hashlib.sha256(original).hexdigest()
    assert headers["etag"] == etag_of(original)
    assert headers["cache-control"] == PUBLIC_CACHE


@pytest.mark.asyncio
async def test_the_downloaded_bytes_open_as_the_same_package(api: Api, catalog: Catalog):
    response = await api.get(DOWNLOAD)

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.testzip() is None
        assert "SKILL.md" in archive.namelist()
        assert archive.read("docs/guide.md") == b"# Guide\n\nHello there\n"


@pytest.mark.asyncio
async def test_downloading_latest_serves_the_resolved_version(api: Api, catalog: Catalog):
    response = await api.get(f"/skills/{PUBLISHED_ENTRY}/versions/latest/download")

    assert response.content == catalog.zip_of(PUBLISHED_ENTRY, "1.1.0")
    assert response.headers["content-disposition"] == (
        f'attachment; filename="{PUBLISHED_ENTRY}-1.1.0.zip"'
    )


@pytest.mark.asyncio
async def test_the_url_advertised_in_the_version_detail_downloads_the_package(
    api: Api, catalog: Catalog
):
    detail = (await api.get(f"/skills/{PUBLISHED_ENTRY}/versions/1.0.0")).json()
    assert detail["download_url"].startswith(PUBLIC_URL)

    response = await api.get(detail["download_url"].removeprefix(PUBLIC_URL))

    assert response.status_code == 200
    assert hashlib.sha256(response.content).hexdigest() == detail["sha256"]


@pytest.mark.asyncio
async def test_a_deprecated_version_is_still_downloadable_by_its_number(api: Api, catalog: Catalog):
    response = await api.get(f"/skills/{PUBLISHED_ENTRY}/versions/0.9.0/download")

    assert response.status_code == 200
    assert response.content == catalog.zip_of(PUBLISHED_ENTRY, "0.9.0")
    assert response.headers["cache-control"] == PUBLIC_CACHE


@pytest.mark.asyncio
async def test_a_package_comes_back_exactly_as_it_was_uploaded_even_with_junk_and_wrapper(
    api: Api, catalog: Catalog
):
    original = make_skill_zip(
        "wrapped-skill", "1.0.0", wrapper="wrapped-skill", macos_junk=True, dir_entries=True
    )
    uploaded = await api.upload(original, "owner", publish=True)
    assert uploaded.status_code == 201

    response = await api.get("/skills/wrapped-skill/versions/1.0.0/download")

    assert response.content == original
    assert response.headers["x-content-sha256"] == uploaded.json()["version"]["sha256"]


@pytest.mark.asyncio
async def test_a_package_larger_than_one_stream_chunk_is_not_altered(api: Api, catalog: Catalog):
    blob = random.Random(7).randbytes(400 * 1024)
    original = make_skill_zip("chunky-skill", "1.0.0", files={"payload.bin": blob})
    await api.upload(original, "owner", publish=True)

    response = await api.get("/skills/chunky-skill/versions/1.0.0/download")

    assert len(original) > 256 * 1024
    assert response.content == original
    assert response.headers["content-length"] == str(len(original))


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["owner", "owner_read", "admin", "admin_read"])
async def test_a_draft_download_is_private_and_not_cacheable(
    api: Api, catalog: Catalog, actor: str
):
    response = await api.get(f"/skills/{DRAFT_ENTRY}/versions/0.1.0/download", actor)

    assert response.status_code == 200
    assert response.headers["cache-control"] == PRIVATE_CACHE


@pytest.mark.asyncio
async def test_a_hidden_entry_downloads_privately_for_its_owner(api: Api, catalog: Catalog):
    response = await api.get(f"/skills/{HIDDEN_ENTRY}/versions/1.0.0/download", "owner")

    assert response.status_code == 200
    assert response.headers["cache-control"] == PRIVATE_CACHE


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["stranger", "owner_read", "admin"])
async def test_with_private_reading_every_download_is_private(
    api: Api, catalog: Catalog, monkeypatch: pytest.MonkeyPatch, actor: str
):
    monkeypatch.setattr(settings.registry, "public_read", False)

    response = await api.get(DOWNLOAD, actor)

    assert response.status_code == 200
    assert response.headers["cache-control"] == PRIVATE_CACHE


@pytest.mark.asyncio
async def test_an_authenticated_download_of_a_public_version_is_still_public(
    api: Api, catalog: Catalog
):
    response = await api.get(DOWNLOAD, "stranger")

    assert response.headers["cache-control"] == PUBLIC_CACHE


@pytest.mark.asyncio
async def test_a_matching_etag_answers_not_modified_without_a_body(api: Api, catalog: Catalog):
    original = catalog.zip_of(PUBLISHED_ENTRY, "1.0.0")

    response = await api.get(DOWNLOAD, headers={"If-None-Match": etag_of(original)})

    assert response.status_code == 304
    assert response.content == b""
    assert response.headers["etag"] == etag_of(original)
    assert response.headers["cache-control"] == PUBLIC_CACHE
    assert response.headers["x-content-sha256"] == hashlib.sha256(original).hexdigest()
    assert "content-length" not in response.headers or response.headers["content-length"] == "0"
    assert "content-disposition" not in response.headers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "template",
    [
        "{etag}",
        "W/{etag}",
        '"unrelated", {etag}',
        '{etag}, "unrelated"',
        '"a" , W/{etag} , "b"',
        "*",
    ],
)
async def test_every_form_of_if_none_match_that_names_the_etag_matches(
    api: Api, catalog: Catalog, template: str
):
    original = catalog.zip_of(PUBLISHED_ENTRY, "1.0.0")

    response = await api.get(
        DOWNLOAD, headers={"If-None-Match": template.format(etag=etag_of(original))}
    )

    assert response.status_code == 304


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [
        '"0000000000000000000000000000000000000000000000000000000000000000"',
        "garbage",
        "",
        "W/",
        hashlib.sha256(b"x").hexdigest(),
    ],
)
async def test_an_if_none_match_that_does_not_name_the_etag_gets_the_package(
    api: Api, catalog: Catalog, value: str
):
    response = await api.get(DOWNLOAD, headers={"If-None-Match": value})

    assert response.status_code == 200
    assert len(response.content) > 0


@pytest.mark.asyncio
async def test_an_etag_from_a_different_version_does_not_match(api: Api, catalog: Catalog):
    other = catalog.zip_of(PUBLISHED_ENTRY, "1.1.0")

    response = await api.get(DOWNLOAD, headers={"If-None-Match": etag_of(other)})

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_a_private_download_answers_not_modified_with_private_caching(
    api: Api, catalog: Catalog
):
    original = catalog.zip_of(DRAFT_ENTRY, "0.1.0")

    response = await api.get(
        f"/skills/{DRAFT_ENTRY}/versions/0.1.0/download",
        "owner",
        headers={"If-None-Match": etag_of(original)},
    )

    assert response.status_code == 304
    assert response.headers["cache-control"] == PRIVATE_CACHE


@pytest.mark.asyncio
async def test_a_matching_etag_never_bypasses_visibility(api: Api, catalog: Catalog):
    original = catalog.zip_of(DRAFT_ENTRY, "0.1.0")
    headers = {"If-None-Match": etag_of(original)}

    anonymous = await api.get(f"/skills/{DRAFT_ENTRY}/versions/0.1.0/download", headers=headers)
    stranger = await api.get(
        f"/skills/{DRAFT_ENTRY}/versions/0.1.0/download", "stranger", headers=headers
    )
    star = await api.get(
        f"/skills/{DRAFT_ENTRY}/versions/0.1.0/download",
        headers={"If-None-Match": "*"},
    )

    for response in (anonymous, stranger, star):
        assert_error(response, 404, "NOT_FOUND")


@pytest.mark.asyncio
async def test_a_matching_etag_never_bypasses_authentication(
    api: Api, catalog: Catalog, monkeypatch: pytest.MonkeyPatch
):
    original = catalog.zip_of(PUBLISHED_ENTRY, "1.0.0")
    monkeypatch.setattr(settings.registry, "public_read", False)

    anonymous = await api.get(DOWNLOAD, headers={"If-None-Match": etag_of(original)})
    invalid = await api.get(
        DOWNLOAD,
        headers={"If-None-Match": etag_of(original), "Authorization": "Bearer nope"},
    )

    assert_error(anonymous, 401, "UNAUTHORIZED")
    assert_error(invalid, 401, "UNAUTHORIZED")


@pytest.mark.asyncio
async def test_a_missing_blob_is_a_generic_500_that_names_no_path(
    api: Api, catalog: Catalog, blob_store, caplog: pytest.LogCaptureFixture
):
    sha = hashlib.sha256(catalog.zip_of(PUBLISHED_ENTRY, "1.0.0")).hexdigest()
    blob_store._path(sha).unlink()

    with caplog.at_level(logging.ERROR):
        response = await api.get(DOWNLOAD)

    error = assert_error(response, 500, "STORAGE_ERROR")
    assert_no_leak(response, str(blob_store.root), sha)
    assert error["message"] == "The stored package data could not be served"
    assert any(
        record.levelno == logging.ERROR and "Storage error" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_a_blob_with_the_wrong_size_is_a_generic_500(api: Api, catalog: Catalog, blob_store):
    sha = hashlib.sha256(catalog.zip_of(PUBLISHED_ENTRY, "1.0.0")).hexdigest()
    path = blob_store._path(sha)
    path.write_bytes(path.read_bytes()[:-7])

    response = await api.get(DOWNLOAD)

    assert_error(response, 500, "STORAGE_ERROR")
    assert_no_leak(response, str(blob_store.root))


@pytest.mark.asyncio
async def test_a_corrupted_blob_of_the_right_size_is_detectable_and_logged(
    lenient_api: Api, catalog: Catalog, blob_store, caplog: pytest.LogCaptureFixture
):
    original = catalog.zip_of(PUBLISHED_ENTRY, "1.0.0")
    path = blob_store._path(hashlib.sha256(original).hexdigest())
    damaged = bytearray(original)
    damaged[40] ^= 0xFF
    path.write_bytes(bytes(damaged))

    with caplog.at_level(logging.ERROR):
        response = await lenient_api.get(DOWNLOAD)

    assert hashlib.sha256(response.content).hexdigest() != response.headers["x-content-sha256"]
    assert any("does not match its hash" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_files_of_a_missing_blob_are_also_a_generic_500(
    api: Api, catalog: Catalog, blob_store
):
    sha = hashlib.sha256(catalog.zip_of(PUBLISHED_ENTRY, "1.0.0")).hexdigest()
    blob_store._path(sha).unlink()

    for suffix in ("entrypoint", "files/docs/guide.md", "raw/docs/guide.md"):
        response = await api.get(f"/skills/{PUBLISHED_ENTRY}/versions/1.0.0/{suffix}")
        assert_error(response, 500, "STORAGE_ERROR")
        assert_no_leak(response, str(blob_store.root))

    listing = await api.get(f"/skills/{PUBLISHED_ENTRY}/versions/1.0.0/files")
    assert listing.status_code == 200


@pytest.mark.asyncio
async def test_a_client_that_leaves_a_download_midway_releases_the_stored_file(
    api: Api, catalog: Catalog, blob_store, monkeypatch: pytest.MonkeyPatch
):
    payload = random.Random(11).randbytes(3 * 1024 * 1024)
    original = make_skill_zip(
        "leaving-client", "1.0.0", files={"payload.bin": payload}, compression=zipfile.ZIP_STORED
    )
    await api.upload(original, "owner", publish=True)
    handles = []
    real_open = Path.open

    def recording_open(self, *args, **kwargs):
        handle = real_open(self, *args, **kwargs)
        if str(self).startswith(str(blob_store.root)):
            handles.append(handle)
        return handle

    monkeypatch.setattr(Path, "open", recording_open)

    outcome = await stream_through_the_app(
        "/api/v1/skills/leaving-client/versions/1.0.0/download", disconnect_after=2
    )
    await asyncio.sleep(0.2)

    assert outcome.status == 200
    assert 0 < outcome.received < len(original)
    assert len(handles) == 1
    assert handles[0].closed


@pytest.mark.asyncio
async def test_latest_is_cached_only_if_it_is_revalidated_with_its_etag(api: Api, catalog: Catalog):
    original = catalog.zip_of(PUBLISHED_ENTRY, "1.1.0")

    response = await api.get(LATEST_DOWNLOAD)

    assert response.status_code == 200
    assert response.headers["cache-control"] == REVALIDATE_CACHE
    assert response.headers["etag"] == etag_of(original)
    assert response.headers["x-content-sha256"] == hashlib.sha256(original).hexdigest()


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["0.9.0", "1.0.0", "1.1.0", "2.0.0-beta.1"])
async def test_an_exact_version_is_immutable_whatever_its_state(
    api: Api, catalog: Catalog, version: str
):
    response = await api.get(f"/skills/{PUBLISHED_ENTRY}/versions/{version}/download")

    assert response.status_code == 200
    assert response.headers["cache-control"] == PUBLIC_CACHE


@pytest.mark.asyncio
async def test_the_exact_version_that_latest_resolves_to_is_still_immutable(
    api: Api, catalog: Catalog
):
    latest = await api.get(LATEST_DOWNLOAD)
    exact = await api.get(f"/skills/{PUBLISHED_ENTRY}/versions/1.1.0/download")

    assert latest.content == exact.content
    assert latest.headers["etag"] == exact.headers["etag"]
    assert (latest.headers["cache-control"], exact.headers["cache-control"]) == (
        REVALIDATE_CACHE,
        PUBLIC_CACHE,
    )


@pytest.mark.asyncio
async def test_a_matching_etag_on_latest_answers_not_modified_with_the_same_policy(
    api: Api, catalog: Catalog
):
    original = catalog.zip_of(PUBLISHED_ENTRY, "1.1.0")

    response = await api.get(LATEST_DOWNLOAD, headers={"If-None-Match": etag_of(original)})

    assert response.status_code == 304
    assert response.content == b""
    assert response.headers["cache-control"] == REVALIDATE_CACHE
    assert response.headers["etag"] == etag_of(original)


@pytest.mark.asyncio
async def test_the_etag_of_an_older_latest_stops_matching_when_a_newer_version_is_published(
    api: Api, catalog: Catalog
):
    stale = etag_of(catalog.zip_of(PUBLISHED_ENTRY, "1.1.0"))
    newer = make_skill_zip(PUBLISHED_ENTRY, "1.2.0", body="# newer\n")
    uploaded = await api.upload(newer, "owner", publish=True)
    assert uploaded.status_code == 201

    response = await api.get(LATEST_DOWNLOAD, headers={"If-None-Match": stale})

    assert response.status_code == 200
    assert response.content == newer
    assert response.headers["etag"] == etag_of(newer)
    assert response.headers["cache-control"] == REVALIDATE_CACHE


@pytest.mark.asyncio
async def test_an_authenticated_caller_gets_the_same_policy_for_a_public_latest(
    api: Api, catalog: Catalog
):
    response = await api.get(LATEST_DOWNLOAD, "stranger")

    assert response.headers["cache-control"] == REVALIDATE_CACHE


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["owner", "owner_read", "admin"])
async def test_the_latest_of_a_hidden_entry_is_private(api: Api, catalog: Catalog, actor: str):
    response = await api.get(f"/skills/{HIDDEN_ENTRY}/versions/latest/download", actor)

    assert response.status_code == 200
    assert response.headers["cache-control"] == PRIVATE_CACHE


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["stranger", "owner_read", "admin"])
async def test_with_private_reading_latest_is_private_too(
    api: Api, catalog: Catalog, monkeypatch: pytest.MonkeyPatch, actor: str
):
    monkeypatch.setattr(settings.registry, "public_read", False)

    response = await api.get(LATEST_DOWNLOAD, actor)

    assert response.status_code == 200
    assert response.headers["cache-control"] == PRIVATE_CACHE
    not_modified = await api.get(
        LATEST_DOWNLOAD,
        actor,
        headers={"If-None-Match": response.headers["etag"]},
    )
    assert not_modified.status_code == 304
    assert not_modified.headers["cache-control"] == PRIVATE_CACHE
