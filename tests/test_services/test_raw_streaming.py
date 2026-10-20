from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import time
import tracemalloc
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest
import pytest_asyncio

from app.repositories import skill_repo
from app.services.errors import NotFoundError, StorageError, ValidationError
from app.services.package_service import PackageService, RawFile
from tests.helpers.handles import CountingHandle, RecordingBlobStore, SlowHandle
from tests.helpers.packages import (
    corrupt_entry_data,
    make_grounding_zip,
    make_skill_zip,
    make_zeros_zip,
    skill_md,
)

KIB = 1024
MIB = 1024 * KIB
CHUNK = 64 * KIB
PUBLIC_URL = "https://registry.example.com"

TEXT = "".join(f"{n:06d} {(n * 2654435761) % 1000003:07d} alpha beta\n" for n in range(30_000))
RANDOM = os.urandom(3 * MIB)


def sha(content: bytes | str) -> str:
    raw = content.encode("utf-8") if isinstance(content, str) else content
    return hashlib.sha256(raw).hexdigest()


async def drain(stream) -> bytes:
    return b"".join([chunk async for chunk in stream])


async def wait_for(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("the condition never became true")
        await asyncio.sleep(0.005)


def blob_path(store, digest: str) -> Path:
    return store.root / digest[:2] / f"{digest}.zip"


def make_service(db_session, store) -> PackageService:
    return PackageService(
        db_session, store, public_read=True, auto_publish=False, public_url=PUBLIC_URL
    )


def counting(handle, _number):
    return CountingHandle(handle)


@pytest.fixture
def service(db_session, blob_store):
    return make_service(db_session, blob_store)


@pytest_asyncio.fixture
async def big(service, actors):
    data = make_skill_zip(
        "big-files",
        "1.0.0",
        files={"text.txt": TEXT, "random.bin": RANDOM, "empty.txt": "", "small.txt": "tiny\n"},
        compression=zipfile.ZIP_STORED,
    )
    result = await service.upload(actors.owner, data, publish=True)
    return result.version


@pytest_asyncio.fixture
async def deflated(service, actors):
    data = make_skill_zip(
        "deflated-files",
        "1.0.0",
        files={"text.txt": TEXT, "random.bin": RANDOM, "small.txt": "tiny\n"},
    )
    result = await service.upload(actors.owner, data, publish=True)
    return result.version


async def tamper_manifest(db_session, name: str, version: str, mutate) -> None:
    skill = await skill_repo.get_skill_by_name(db_session, name)
    target = await skill_repo.get_version(db_session, skill.id, version)
    manifest = json.loads(target.manifest)
    mutate(manifest)
    target.manifest = json.dumps(manifest)
    await db_session.commit()


class TestStreamedContent:
    @pytest.mark.parametrize("compression", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
    @pytest.mark.parametrize("wrapper", [None, "pkg"])
    async def test_every_file_streams_back_byte_for_byte(
        self, service, actors, compression, wrapper
    ):
        files = {
            "docs/guide.md": TEXT[:50_000],
            "data/table.bin": bytes(range(256)) * 700,
            "empty.txt": "",
            "notes/ünïcode.txt": "héllo wörld\n",
            "random.bin": os.urandom(400 * KIB),
        }
        data = make_skill_zip(
            "round-trip", "1.0.0", files=files, compression=compression, wrapper=wrapper
        )
        await service.upload(actors.owner, data, publish=True)
        listing = await service.manifest(None, "round-trip", "1.0.0")

        assert {entry.path for entry in listing.files} == {"SKILL.md", *files}
        for entry in listing.files:
            raw = await service.open_raw(None, "round-trip", "1.0.0", entry.path)
            content = await drain(raw.stream)
            assert (raw.path, raw.size) == (entry.path, entry.size)
            assert (raw.media_type, raw.text) == (entry.media_type, entry.text)
            assert len(content) == entry.size and sha(content) == entry.sha256

    async def test_the_result_describes_the_file_without_holding_its_bytes(self, service, big):
        raw = await service.open_raw(None, "big-files", "1.0.0", "random.bin")

        assert isinstance(raw, RawFile)
        assert (raw.path, raw.size, raw.text) == ("random.bin", len(RANDOM), False)
        assert not hasattr(raw, "data")
        await raw.stream.aclose()

    async def test_chunks_are_bounded_and_there_are_several_for_a_large_file(self, service, big):
        raw = await service.open_raw(None, "big-files", "1.0.0", "random.bin")

        chunks = [chunk async for chunk in raw.stream]

        assert b"".join(chunks) == RANDOM
        assert len(chunks) >= len(RANDOM) // CHUNK
        assert all(0 < len(chunk) <= CHUNK for chunk in chunks)

    async def test_a_small_file_arrives_as_one_chunk_and_an_empty_one_as_none(self, service, big):
        small = await service.open_raw(None, "big-files", "1.0.0", "small.txt")
        empty = await service.open_raw(None, "big-files", "1.0.0", "empty.txt")

        assert [chunk async for chunk in small.stream] == [b"tiny\n"]
        assert [chunk async for chunk in empty.stream] == []
        assert empty.size == 0

    async def test_a_file_can_be_requested_again_and_yields_the_same_bytes(self, service, big):
        first = await service.open_raw(None, "big-files", "1.0.0", "text.txt")
        second = await service.open_raw(None, "big-files", "1.0.0", "text.txt")

        assert await drain(first.stream) == TEXT.encode() == await drain(second.stream)

    async def test_the_inline_limit_does_not_apply(self, db_session, blob_store, big):
        tiny = PackageService(db_session, blob_store, max_inline_read_bytes=10)

        raw = await tiny.open_raw(None, "big-files", "1.0.0", "text.txt")

        assert await drain(raw.stream) == TEXT.encode()

    async def test_a_grounding_entry_point_streams_like_any_other_file(self, service, actors):
        data = make_grounding_zip("ground", "1.0.0", body="# Index\n" * 5000)
        await service.upload(actors.owner, data, publish=True)

        raw = await service.open_raw(None, "ground", "latest", "INDEX.md")
        entry = await service.entrypoint(None, "ground")

        assert await drain(raw.stream) == entry.content.encode()

    async def test_the_stream_does_not_depend_on_the_database_session(
        self, service, db_session, big
    ):
        raw = await service.open_raw(None, "big-files", "1.0.0", "random.bin")
        await db_session.close()

        assert await drain(raw.stream) == RANDOM


class TestIntegrityBeforeTheFirstByte:
    async def test_corruption_in_the_last_byte_of_a_stored_file_is_found_by_open_raw(
        self, service, blob_store, big, caplog
    ):
        path = blob_path(blob_store, big.sha256)
        path.write_bytes(corrupt_entry_data(path.read_bytes(), "random.bin", index=len(RANDOM) - 1))

        with (
            caplog.at_level(logging.ERROR, logger="app.services.package"),
            pytest.raises(StorageError) as caught,
        ):
            await service.open_raw(None, "big-files", "1.0.0", "random.bin")

        assert caught.value.message == "The stored package cannot be read"
        logged = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert len(logged) == 1
        assert "random.bin cannot be read from the package" in logged[0]
        untouched = await service.open_raw(None, "big-files", "1.0.0", "small.txt")
        assert await drain(untouched.stream) == b"tiny\n"

    async def test_corruption_in_the_middle_of_a_deflated_file_is_found_by_open_raw(
        self, service, blob_store, deflated
    ):
        path = blob_path(blob_store, deflated.sha256)
        path.write_bytes(corrupt_entry_data(path.read_bytes(), "text.txt", index=2000))

        with pytest.raises(StorageError):
            await service.open_raw(None, "deflated-files", "1.0.0", "text.txt")

    async def test_a_file_that_differs_from_its_manifest_hash_is_found_by_open_raw(
        self, service, db_session, big, caplog
    ):
        def poison(manifest):
            for entry in manifest:
                if entry["path"] == "text.txt":
                    entry["sha256"] = "0" * 64

        await tamper_manifest(db_session, "big-files", "1.0.0", poison)

        with (
            caplog.at_level(logging.ERROR, logger="app.services.package"),
            pytest.raises(StorageError) as caught,
        ):
            await service.open_raw(None, "big-files", "1.0.0", "text.txt")

        assert caught.value.message == "The stored file does not match its manifest entry"
        mismatches = [r for r in caplog.records if "does not match its manifest entry" in r.message]
        assert len(mismatches) == 1

    @pytest.mark.parametrize("delta", [-1, 1, 10_000, -50_000])
    async def test_a_wrong_size_in_the_manifest_is_found_by_open_raw(
        self, service, db_session, big, delta
    ):
        def resize(manifest):
            for entry in manifest:
                if entry["path"] == "text.txt":
                    entry["size"] += delta

        await tamper_manifest(db_session, "big-files", "1.0.0", resize)

        with pytest.raises(StorageError) as caught:
            await service.open_raw(None, "big-files", "1.0.0", "text.txt")

        assert caught.value.message == "The stored file does not match its manifest entry"

    async def test_a_missing_blob_is_found_without_leaving_a_handle_behind(
        self, db_session, blob_store, big, caplog
    ):
        recording = RecordingBlobStore(blob_store)
        service = make_service(db_session, recording)
        await blob_store.delete(big.sha256)

        with (
            caplog.at_level(logging.ERROR, logger="app.services.package"),
            pytest.raises(StorageError) as caught,
        ):
            await service.open_raw(None, "big-files", "1.0.0", "text.txt")

        assert caught.value.message == "The stored package for this version is unavailable"
        assert recording.handles == []
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert [r.extra_data["sha256"] for r in errors] == [big.sha256]

    @pytest.mark.parametrize("failure", ["hash", "crc", "ghost"])
    async def test_every_failed_verification_closes_the_handle_it_opened(
        self, db_session, blob_store, big, failure
    ):
        recording = RecordingBlobStore(blob_store, wrap=counting)
        service = make_service(db_session, recording)
        target = "text.txt"
        if failure == "crc":
            path = blob_path(blob_store, big.sha256)
            path.write_bytes(corrupt_entry_data(path.read_bytes(), "text.txt", index=100))
        elif failure == "hash":

            def poison(manifest):
                for entry in manifest:
                    entry["sha256"] = "f" * 64

            await tamper_manifest(db_session, "big-files", "1.0.0", poison)
        else:

            def ghost(manifest):
                manifest.append(
                    {
                        "path": "ghost.txt",
                        "size": 3,
                        "sha256": sha("abc"),
                        "media_type": "text/plain",
                        "text": True,
                    }
                )

            await tamper_manifest(db_session, "big-files", "1.0.0", ghost)
            target = "ghost.txt"

        with pytest.raises(StorageError):
            await service.open_raw(None, "big-files", "1.0.0", target)

        assert len(recording.handles) == 1
        assert recording.all_closed()

    async def test_paths_outside_the_manifest_never_open_the_blob(
        self, db_session, blob_store, big
    ):
        recording = RecordingBlobStore(blob_store)
        service = make_service(db_session, recording)

        for path in ("nope.txt", "TEXT.TXT", "text.txt.bak"):
            with pytest.raises(NotFoundError):
                await service.open_raw(None, "big-files", "1.0.0", path)
        for path in ("../x", "/etc/passwd", "text.txt/", "a\\b", "a\x00b", ""):
            with pytest.raises(ValidationError) as caught:
                await service.open_raw(None, "big-files", "1.0.0", path)
            assert caught.value.reason == "INVALID_PATH"
        with pytest.raises(NotFoundError):
            await service.open_raw(None, "big-files", "9.9.9", "text.txt")
        with pytest.raises(NotFoundError):
            await service.open_raw(None, "ghost-entry", "1.0.0", "text.txt")

        assert recording.handles == []


class TestStreamLifecycle:
    @pytest.fixture
    def recording(self, blob_store):
        return RecordingBlobStore(blob_store, wrap=counting)

    async def test_nothing_stays_open_between_verification_and_streaming(
        self, db_session, recording, big
    ):
        service = make_service(db_session, recording)

        raw = await service.open_raw(None, "big-files", "1.0.0", "random.bin")

        assert len(recording.handles) == 1
        assert recording.all_closed()
        assert recording.whole_reads == 0 and recording.streams == 0
        await raw.stream.aclose()
        assert len(recording.handles) == 1

    async def test_consuming_the_stream_opens_one_more_handle_and_closes_it(
        self, db_session, recording, big
    ):
        service = make_service(db_session, recording)
        raw = await service.open_raw(None, "big-files", "1.0.0", "random.bin")

        content = await drain(raw.stream)

        assert content == RANDOM
        assert len(recording.handles) == 2
        assert recording.all_closed()
        assert recording.opened_keys == [big.sha256, big.sha256]

    async def test_the_verification_pass_and_the_streaming_pass_read_the_file_once_each(
        self, db_session, recording, big
    ):
        service = make_service(db_session, recording)
        raw = await service.open_raw(None, "big-files", "1.0.0", "random.bin")
        verification = recording.handles[0].bytes_read
        await drain(raw.stream)
        streaming = recording.handles[1].bytes_read

        assert len(RANDOM) <= verification < len(RANDOM) + 256 * KIB
        assert len(RANDOM) <= streaming < len(RANDOM) + 256 * KIB

    async def test_abandoning_the_stream_after_one_chunk_closes_the_handle(
        self, db_session, recording, big
    ):
        service = make_service(db_session, recording)
        raw = await service.open_raw(None, "big-files", "1.0.0", "random.bin")

        first = await raw.stream.__anext__()
        assert not recording.handles[1].closed
        await raw.stream.aclose()

        assert RANDOM.startswith(first)
        assert recording.handles[1].closed
        assert recording.handles[1].bytes_read < len(RANDOM) // 2

    async def test_a_consumer_that_fails_still_releases_the_handle(
        self, db_session, recording, big
    ):
        service = make_service(db_session, recording)
        raw = await service.open_raw(None, "big-files", "1.0.0", "random.bin")

        with pytest.raises(RuntimeError, match="client went away"):
            try:
                async for _ in raw.stream:
                    raise RuntimeError("client went away")
            finally:
                await raw.stream.aclose()

        assert recording.all_closed()

    async def test_a_stream_that_never_started_never_opens_anything(
        self, db_session, recording, big
    ):
        service = make_service(db_session, recording)
        raw = await service.open_raw(None, "big-files", "1.0.0", "random.bin")

        await raw.stream.aclose()

        assert len(recording.handles) == 1

    @pytest.mark.parametrize("reads_before_cancelling", [0, 4, 9])
    async def test_cancelling_the_consumer_while_a_worker_is_reading_ends_quietly(
        self, db_session, blob_store, big, caplog, reads_before_cancelling
    ):
        slow: list[SlowHandle] = []

        def slow_streaming_pass(handle, number):
            if number == 0:
                return CountingHandle(handle)
            made = SlowHandle(handle, 0.05)
            slow.append(made)
            return made

        recording = RecordingBlobStore(blob_store, wrap=slow_streaming_pass)
        service = make_service(db_session, recording)
        raw = await service.open_raw(None, "big-files", "1.0.0", "random.bin")
        unhandled: list[dict] = []
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, context: unhandled.append(context)
        )

        task = asyncio.create_task(drain(raw.stream))
        await wait_for(lambda: bool(slow) and slow[0].in_flight.is_set())
        await wait_for(lambda: slow[0].reads >= reads_before_cancelling)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.6)

        assert recording.all_closed()
        assert unhandled == []
        assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


class TestTheBlobChangesAfterVerification:
    @staticmethod
    async def serve_replacement(db_session, blob_store, replacement: bytes) -> PackageService:
        key = sha(replacement)
        await blob_store.put(key, replacement)
        return make_service(db_session, RecordingBlobStore(blob_store, substitute=(1, key)))

    @pytest_asyncio.fixture
    async def original(self, db_session, blob_store, actors):
        data = make_skill_zip(
            "swap-me",
            "1.0.0",
            files={"payload.bin": RANDOM[: 1 * MIB]},
            compression=zipfile.ZIP_STORED,
        )
        await make_service(db_session, blob_store).upload(actors.owner, data, publish=True)

    async def test_a_replacement_with_the_same_size_is_detected_before_the_stream_ends(
        self, db_session, blob_store, original
    ):
        other = os.urandom(1 * MIB)
        replacement = make_skill_zip(
            "swap-me", "1.0.0", files={"payload.bin": other}, compression=zipfile.ZIP_STORED
        )
        service = await self.serve_replacement(db_session, blob_store, replacement)
        raw = await service.open_raw(None, "swap-me", "1.0.0", "payload.bin")
        received = bytearray()

        with pytest.raises(StorageError) as caught:
            async for chunk in raw.stream:
                received += chunk

        assert caught.value.message == "The stored file does not match its manifest entry"
        assert bytes(received) == other

    @pytest.mark.parametrize("extra", [1, 4096, 3 * MIB])
    async def test_a_longer_replacement_never_gets_past_the_declared_size(
        self, db_session, blob_store, original, extra
    ):
        replacement = make_skill_zip(
            "swap-me",
            "1.0.0",
            files={"payload.bin": os.urandom(1 * MIB + extra)},
            compression=zipfile.ZIP_STORED,
        )
        service = await self.serve_replacement(db_session, blob_store, replacement)
        raw = await service.open_raw(None, "swap-me", "1.0.0", "payload.bin")
        received = 0

        with pytest.raises(StorageError):
            async for chunk in raw.stream:
                received += len(chunk)

        assert received <= raw.size

    async def test_a_shorter_replacement_is_detected_at_the_end(
        self, db_session, blob_store, original
    ):
        replacement = make_skill_zip(
            "swap-me",
            "1.0.0",
            files={"payload.bin": os.urandom(100 * KIB)},
            compression=zipfile.ZIP_STORED,
        )
        service = await self.serve_replacement(db_session, blob_store, replacement)
        raw = await service.open_raw(None, "swap-me", "1.0.0", "payload.bin")
        received = 0

        with pytest.raises(StorageError):
            async for chunk in raw.stream:
                received += len(chunk)

        assert received == 100 * KIB

    async def test_a_replacement_that_lost_the_file_is_a_storage_error(
        self, db_session, blob_store, original
    ):
        replacement = make_skill_zip("swap-me", "1.0.0", files={"other.bin": b"x"})
        service = await self.serve_replacement(db_session, blob_store, replacement)
        raw = await service.open_raw(None, "swap-me", "1.0.0", "payload.bin")

        with pytest.raises(StorageError) as caught:
            await drain(raw.stream)

        assert caught.value.message == "The stored package cannot be read"


class TestMemoryStaysFlat:
    @staticmethod
    async def upload_zeros(service, actors, name: str, megabytes: int) -> None:
        data = make_zeros_zip(megabytes * MIB, name=name)
        assert len(data) < 2 * MIB
        await service.upload(actors.owner, data, publish=True)

    async def test_verifying_and_streaming_a_huge_compressible_file_allocates_little(
        self, service, actors
    ):
        await self.upload_zeros(service, actors, "zeros-big", 48)
        total = 0
        digest = hashlib.sha256()

        tracemalloc.start()
        try:
            raw = await service.open_raw(None, "zeros-big", "1.0.0", "payload.bin")
            async for chunk in raw.stream:
                total += len(chunk)
                digest.update(chunk)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

        assert raw.size == total == 48 * MIB
        assert digest.hexdigest() == sha(bytes(48 * MIB))
        assert peak < 3 * MIB

    async def test_many_concurrent_readers_of_one_huge_file_do_not_add_up_to_its_size(
        self, session_factory, blob_store, actors
    ):
        async with session_factory() as session:
            await self.upload_zeros(make_service(session, blob_store), actors, "zeros-many", 24)

        async def read_all(raw) -> int:
            total = 0
            async for chunk in raw.stream:
                total += len(chunk)
            return total

        tracemalloc.start()
        try:
            streams = []
            for _ in range(12):
                async with session_factory() as session:
                    streams.append(
                        await make_service(session, blob_store).open_raw(
                            None, "zeros-many", "1.0.0", "payload.bin"
                        )
                    )
            sizes = await asyncio.gather(*[read_all(raw) for raw in streams])
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

        assert sizes == [24 * MIB] * 12
        assert peak < 24 * MIB

    async def test_a_prefix_of_a_huge_file_is_read_without_inflating_the_rest(
        self, service, db_session, blob_store, actors
    ):
        unit = b"abcdefghi\n"
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("SKILL.md", skill_md("repeated-text", "1.0.0"))
            with archive.open("payload.txt", "w") as member:
                block = unit * (1 * MIB // len(unit))
                for _ in range(48):
                    member.write(block)
        await service.upload(actors.owner, buffer.getvalue(), publish=True)
        small = PackageService(db_session, blob_store, public_read=True, max_inline_read_bytes=1000)

        tracemalloc.start()
        try:
            content = await small.read_file(None, "repeated-text", "1.0.0", "payload.txt")
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

        assert content.truncated is True
        assert content.size == 48 * (1 * MIB // len(unit)) * len(unit)
        assert content.content == (unit * 100)[:1000].decode()
        assert peak < 2 * MIB


class TestAMemberLargerThanItsManifestEntryIsNeverInflated:
    @pytest_asyncio.fixture
    async def liar(self, service, actors, db_session):
        data = make_zeros_zip(48 * MIB, name="zeros-liar")
        await service.upload(actors.owner, data, publish=True)

        def understate(manifest):
            for entry in manifest:
                if entry["path"] == "payload.bin":
                    entry["size"] = 100
                    entry["text"] = True
                    entry["sha256"] = sha(bytes(100))

        await tamper_manifest(db_session, "zeros-liar", "1.0.0", understate)

    async def test_an_inline_read_stops_at_the_manifest_size(self, service, liar, caplog):
        tracemalloc.start()
        try:
            with (
                caplog.at_level(logging.ERROR, logger="app.services.package"),
                pytest.raises(StorageError) as caught,
            ):
                await service.read_file(None, "zeros-liar", "1.0.0", "payload.bin")
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

        assert caught.value.message == "The stored package cannot be read"
        assert "exceeds the requested limit of 100 bytes" in caplog.records[-1].getMessage()
        assert peak < 2 * MIB

    async def test_a_raw_read_stops_at_the_first_batch_beyond_the_manifest_size(
        self, service, liar
    ):
        tracemalloc.start()
        try:
            with pytest.raises(StorageError) as caught:
                await service.open_raw(None, "zeros-liar", "1.0.0", "payload.bin")
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

        assert caught.value.message == "The stored file does not match its manifest entry"
        assert peak < 2 * MIB


class TestNoReadPathLoadsTheWholePackage:
    @pytest.fixture
    def watched(self, db_session, blob_store):
        recording = RecordingBlobStore(blob_store, wrap=counting)
        return recording, make_service(db_session, recording)

    @pytest_asyncio.fixture
    async def package(self, service, actors):
        data = make_skill_zip(
            "heavy-pkg",
            "1.0.0",
            files={"docs/guide.md": "# Guide\n" * 100, "huge.bin": os.urandom(6 * MIB)},
            compression=zipfile.ZIP_STORED,
        )
        assert len(data) > 6 * MIB
        return (await service.upload(actors.owner, data, publish=True)).version

    async def test_the_entry_point_costs_a_small_slice_of_a_large_package(self, watched, package):
        recording, service = watched

        entry = await service.entrypoint(None, "heavy-pkg")

        assert entry.path == "SKILL.md"
        assert len(recording.handles) == 1
        assert 0 < recording.handles[0].bytes_read < 256 * KIB
        assert recording.all_closed()

    async def test_reading_an_inline_file_costs_a_small_slice_of_a_large_package(
        self, watched, package
    ):
        recording, service = watched

        content = await service.read_file(None, "heavy-pkg", "1.0.0", "docs/guide.md")

        assert content.content == "# Guide\n" * 100
        assert 0 < recording.handles[0].bytes_read < 256 * KIB
        assert recording.all_closed()

    async def test_a_small_raw_file_costs_a_small_slice_in_both_passes(self, watched, package):
        recording, service = watched

        raw = await service.open_raw(None, "heavy-pkg", "1.0.0", "docs/guide.md")
        await drain(raw.stream)

        assert len(recording.handles) == 2
        assert all(0 < handle.bytes_read < 256 * KIB for handle in recording.handles)
        assert recording.all_closed()

    async def test_no_content_operation_uses_the_whole_blob_or_the_download_stream(
        self, watched, package
    ):
        recording, service = watched

        await service.entrypoint(None, "heavy-pkg")
        await service.read_file(None, "heavy-pkg", "1.0.0", "docs/guide.md")
        await service.read_file(None, "heavy-pkg", "1.0.0", "huge.bin")
        await drain((await service.open_raw(None, "heavy-pkg", "1.0.0", "huge.bin")).stream)

        assert recording.whole_reads == 0
        assert recording.streams == 0
        assert recording.all_closed()

    async def test_a_binary_file_is_never_read_for_the_json_view(self, watched, package):
        recording, service = watched

        content = await service.read_file(None, "heavy-pkg", "1.0.0", "huge.bin")

        assert content.content is None and content.text is False
        assert recording.handles == []

    async def test_a_download_still_streams_the_whole_package_from_the_store(
        self, watched, package
    ):
        recording, service = watched

        download = await service.open_download(None, "heavy-pkg", "1.0.0")
        body = await drain(download.stream)

        assert sha(body) == package.sha256
        assert recording.streams == 1
        assert recording.handles == []
