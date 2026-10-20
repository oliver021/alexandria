from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tracemalloc
import zipfile

import pytest

from app.repositories import skill_repo
from app.storage import get_blob_store
from tests.helpers.handles import CountingHandle, RecordingBlobStore
from tests.helpers.packages import corrupt_entry_data, make_skill_zip, make_zeros_zip
from tests.test_api.support import (
    Api,
    Catalog,
    assert_error,
    assert_no_leak,
    stream_through_the_app,
)

MIB = 1024 * 1024
CSP = "default-src 'none'; sandbox"
RAW = "/api/v1/skills/{name}/versions/1.0.0/raw/{path}"
RANDOM = os.urandom(2 * MIB)


@pytest.fixture
async def published(api: Api, catalog: Catalog):
    zeros = make_zeros_zip(24 * MIB, name="zeros-api")
    assert len(zeros) < 2 * MIB
    assert (await api.upload(zeros, "owner", publish=True)).status_code == 201
    stored = make_skill_zip(
        "stored-api",
        "1.0.0",
        files={"random.bin": RANDOM, "docs/readme.txt": "hello\n" * 1000},
        compression=zipfile.ZIP_STORED,
    )
    assert (await api.upload(stored, "owner", publish=True)).status_code == 201
    return zeros, stored


@pytest.mark.asyncio
async def test_a_large_file_is_streamed_with_the_manifest_size_as_content_length(
    api: Api, published
):
    response = await api.get("/skills/stored-api/versions/1.0.0/raw/random.bin")

    assert response.status_code == 200
    assert response.content == RANDOM
    assert response.headers["content-length"] == str(len(RANDOM))
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"] == 'attachment; filename="random.bin"'
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == CSP
    assert "transfer-encoding" not in response.headers


@pytest.mark.asyncio
async def test_text_files_keep_their_inline_plain_text_headers_when_streamed(api: Api, published):
    response = await api.get("/skills/stored-api/versions/1.0.0/raw/docs/readme.txt")

    assert response.status_code == 200
    assert response.text == "hello\n" * 1000
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.headers["content-disposition"] == "inline"
    assert response.headers["content-length"] == str(len("hello\n" * 1000))
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == CSP


@pytest.mark.asyncio
async def test_a_file_of_a_package_with_a_wrapper_directory_streams_too(api: Api, catalog: Catalog):
    data = make_skill_zip(
        "wrapped-raw", "1.0.0", files={"data/blob.bin": RANDOM[:300_000]}, wrapper="wrapped-raw"
    )
    await api.upload(data, "owner", publish=True)

    response = await api.get("/skills/wrapped-raw/versions/1.0.0/raw/data/blob.bin")

    assert response.content == RANDOM[:300_000]


@pytest.mark.asyncio
async def test_the_streamed_body_matches_the_manifest_hash(api: Api, published):
    listing = (await api.get("/skills/stored-api/versions/1.0.0/files")).json()["files"]
    by_path = {entry["path"]: entry for entry in listing}

    for path, entry in by_path.items():
        response = await api.get(f"/skills/stored-api/versions/1.0.0/raw/{path}")
        assert response.status_code == 200
        assert hashlib.sha256(response.content).hexdigest() == entry["sha256"]
        assert int(response.headers["content-length"]) == entry["size"] == len(response.content)


@pytest.mark.asyncio
async def test_a_huge_compressible_file_is_served_in_bounded_memory(api: Api, published):
    path = RAW.format(name="zeros-api", path="payload.bin")
    tracemalloc.start()
    try:
        outcome = await stream_through_the_app(path)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert outcome.status == 200
    assert outcome.headers["content-length"] == str(24 * MIB)
    assert outcome.received == 24 * MIB
    assert outcome.digest.hexdigest() == hashlib.sha256(bytes(24 * MIB)).hexdigest()
    assert outcome.chunks > 24 * MIB // (512 * 1024)
    assert peak < 4 * MIB


@pytest.mark.asyncio
async def test_many_concurrent_requests_for_one_huge_file_stay_bounded(api: Api, published):
    path = RAW.format(name="zeros-api", path="payload.bin")
    tracemalloc.start()
    try:
        outcomes = await asyncio.gather(*[stream_through_the_app(path) for _ in range(16)])
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert {outcome.status for outcome in outcomes} == {200}
    assert {outcome.received for outcome in outcomes} == {24 * MIB}
    assert peak < 32 * MIB


@pytest.mark.asyncio
async def test_corruption_is_a_json_500_before_any_byte_of_the_file_is_sent(
    api: Api, published, blob_store, caplog
):
    stored = published[1]
    digest = hashlib.sha256(stored).hexdigest()
    path = blob_store._path(digest)
    path.write_bytes(corrupt_entry_data(path.read_bytes(), "random.bin", index=len(RANDOM) - 1))

    with caplog.at_level(logging.ERROR):
        response = await api.get("/skills/stored-api/versions/1.0.0/raw/random.bin")
        streamed = await stream_through_the_app(RAW.format(name="stored-api", path="random.bin"))

    assert_error(response, 500, "STORAGE_ERROR")
    assert_no_leak(response, str(blob_store.root))
    assert streamed.status == 500
    assert streamed.received < 400
    assert streamed.first_chunk.startswith(b'{"error"')
    untouched = await api.get("/skills/stored-api/versions/1.0.0/raw/docs/readme.txt")
    assert untouched.status_code == 200


@pytest.mark.asyncio
async def test_a_hash_that_differs_from_the_manifest_is_a_json_500_before_the_body(
    api: Api, published, db_session
):
    skill = await skill_repo.get_skill_by_name(db_session, "stored-api")
    version = await skill_repo.get_version(db_session, skill.id, "1.0.0")
    manifest = json.loads(version.manifest)
    for entry in manifest:
        if entry["path"] == "docs/readme.txt":
            entry["sha256"] = "0" * 64
    version.manifest = json.dumps(manifest)
    await db_session.commit()

    response = await api.get("/skills/stored-api/versions/1.0.0/raw/docs/readme.txt")
    untouched = await api.get("/skills/stored-api/versions/1.0.0/raw/random.bin")

    assert_error(response, 500, "STORAGE_ERROR")
    assert "hello" not in response.text
    assert untouched.status_code == 200 and untouched.content == RANDOM


@pytest.mark.asyncio
async def test_a_client_that_leaves_midway_releases_the_file(api: Api, published, blob_store):
    from app.main import app

    recording = RecordingBlobStore(blob_store, wrap=lambda handle, _: CountingHandle(handle))
    app.dependency_overrides[get_blob_store] = lambda: recording

    outcome = await stream_through_the_app(
        RAW.format(name="zeros-api", path="payload.bin"), disconnect_after=2
    )
    await asyncio.sleep(0.1)

    assert outcome.status == 200
    assert 0 < outcome.received < 24 * MIB
    assert len(recording.handles) == 2
    assert recording.all_closed()


@pytest.mark.asyncio
async def test_a_client_that_leaves_right_after_the_first_chunk_leaves_nothing_open(
    api: Api, published, blob_store
):
    from app.main import app

    recording = RecordingBlobStore(blob_store, wrap=lambda handle, _: CountingHandle(handle))
    app.dependency_overrides[get_blob_store] = lambda: recording

    await stream_through_the_app(
        RAW.format(name="zeros-api", path="payload.bin"), disconnect_after=1
    )
    await asyncio.sleep(0.1)

    assert len(recording.handles) == 2
    assert recording.all_closed()


@pytest.mark.asyncio
async def test_authentication_and_visibility_are_checked_before_the_blob_is_touched(
    api: Api, blob_store
):
    from app.main import app

    recording = RecordingBlobStore(blob_store)
    app.dependency_overrides[get_blob_store] = lambda: recording
    await api.upload(make_skill_zip("draft-raw", "1.0.0", files={"a.txt": "x"}), "owner")

    stranger = await api.get("/skills/draft-raw/versions/1.0.0/raw/a.txt", "stranger")
    anonymous = await api.get("/skills/draft-raw/versions/1.0.0/raw/a.txt")
    owner = await api.get("/skills/draft-raw/versions/1.0.0/raw/a.txt", "owner")

    assert_error(stranger, 404, "NOT_FOUND")
    assert_error(anonymous, 404, "NOT_FOUND")
    assert owner.status_code == 200 and owner.text == "x"
    assert len(recording.handles) == 2
