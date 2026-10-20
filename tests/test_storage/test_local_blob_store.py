from __future__ import annotations

import ast
import asyncio
import errno
import hashlib
import io
import os
import stat
import threading
import time
from contextlib import suppress
from pathlib import Path

import pytest

import app.storage
from app.config.settings import settings
from app.storage import (
    DEFAULT_CHUNK_SIZE,
    BlobIntegrityError,
    BlobNotFound,
    BlobStore,
    BlobStoreError,
    InvalidBlobKey,
    LocalBlobStore,
    get_blob_store,
    reset_blob_store_cache,
    validate_blob_key,
)

BAD_KEYS = {
    "empty": "",
    "short": "abc123",
    "one_char_too_long": "a" * 65,
    "one_char_too_short": "a" * 63,
    "uppercase_hex": "A" * 64,
    "mixed_case_hex": "aB" * 32,
    "non_hex_letters": "g" * 64,
    "trailing_newline": "a" * 64 + "\n",
    "leading_space": " " + "a" * 63,
    "nul_byte": "a" * 63 + "\x00",
    "dotdot_segments": "../" * 21 + "a",
    "parent_prefix": "../" + "a" * 61,
    "slash_inside": "a" * 31 + "/" + "a" * 32,
    "backslash_inside": "a" * 31 + "\\" + "a" * 32,
    "absolute_path": "/etc/passwd",
    "percent_encoded_traversal": "%2e%2e%2f" + "a" * 55,
    "arabic_indic_digits": "٣" * 64,
    "fullwidth_letters": "ａ" * 64,
    "zip_suffix": "a" * 60 + ".zip",
    "algorithm_prefix": "sha256:" + "a" * 57,
    "none": None,
    "integer": 12345,
    "bytes": b"a" * 64,
}

OPERATIONS = {
    "put": lambda store, key: store.put(key, b"data"),
    "get": lambda store, key: store.get(key),
    "open": lambda store, key: store.open(key),
    "exists": lambda store, key: store.exists(key),
    "delete": lambda store, key: store.delete(key),
    "size": lambda store, key: store.size(key),
}


def make_blob(data: bytes) -> tuple[str, bytes]:
    return hashlib.sha256(data).hexdigest(), data


def files_under(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(path for path in root.rglob("*") if path.is_file())


def blob_path(root: Path, key: str) -> Path:
    return root / key[:2] / f"{key}.zip"


async def collect(chunks) -> list[bytes]:
    return [chunk async for chunk in chunks]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "blobs"


@pytest.fixture
def store(root: Path) -> LocalBlobStore:
    return LocalBlobStore(root)


class FailingSecondRead:
    def __init__(self, handle):
        self._handle = handle
        self._reads = 0

    def read(self, size):
        self._reads += 1
        if self._reads > 1:
            raise OSError(errno.EIO, "simulated read failure")
        return self._handle.read(size)

    def close(self):
        self._handle.close()

    @property
    def closed(self):
        return self._handle.closed


@pytest.fixture
def opened_handles(root: Path, monkeypatch: pytest.MonkeyPatch) -> list:
    handles = []
    real_open = Path.open

    def recording_open(self, *args, **kwargs):
        handle = real_open(self, *args, **kwargs)
        if str(self).startswith(str(root)):
            handles.append(handle)
        return handle

    monkeypatch.setattr(Path, "open", recording_open)
    return handles


@pytest.fixture
def io_events(root: Path, monkeypatch: pytest.MonkeyPatch) -> list:
    events = []
    real_fsync = os.fsync
    real_replace = os.replace

    def recording_fsync(descriptor):
        is_directory = stat.S_ISDIR(os.fstat(descriptor).st_mode)
        events.append("fsync-directory" if is_directory else "fsync-file")
        return real_fsync(descriptor)

    def recording_replace(source, destination, *args, **kwargs):
        if str(destination).startswith(str(root)):
            events.append("rename")
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    monkeypatch.setattr(os, "replace", recording_replace)
    return events


@pytest.fixture
def configured_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    target = tmp_path / "configured-blobs"
    monkeypatch.setattr(settings.storage, "path", str(target))
    reset_blob_store_cache()
    yield target
    reset_blob_store_cache()


async def test_put_stores_the_zip_under_a_two_character_shard(store, root):
    key, data = make_blob(b"first package")

    assert await store.put(key, data) is True

    assert files_under(root) == [root / key[:2] / f"{key}.zip"]
    assert blob_path(root, key).read_bytes() == data


async def test_get_exists_and_size_round_trip(store):
    key, data = make_blob(os.urandom(5000))

    assert await store.exists(key) is False
    await store.put(key, data)

    assert await store.exists(key) is True
    assert await store.get(key) == data
    assert await store.size(key) == len(data)


async def test_empty_payload_round_trips(store):
    key, data = make_blob(b"")

    assert await store.put(key, data) is True

    assert await store.get(key) == b""
    assert await store.size(key) == 0
    assert await collect(store.stream(key)) == []


async def test_missing_blob_raises_blob_not_found_for_get_and_size(store):
    key, _ = make_blob(b"never stored")

    with pytest.raises(BlobNotFound):
        await store.get(key)
    with pytest.raises(BlobNotFound):
        await store.size(key)


def test_constructor_does_not_create_the_root(root):
    LocalBlobStore(root)

    assert not root.exists()


async def test_read_operations_on_a_missing_root_do_not_create_it(store, root):
    key, _ = make_blob(b"absent")

    assert await store.exists(key) is False
    assert await store.delete(key) is False
    with pytest.raises(BlobNotFound):
        await store.get(key)
    with pytest.raises(BlobNotFound):
        await store.size(key)
    with pytest.raises(BlobNotFound):
        await collect(store.stream(key))

    assert not root.exists()


async def test_first_put_creates_the_root_lazily(store, root):
    key, data = make_blob(b"lazy")
    assert not root.exists()

    await store.put(key, data)

    assert root.is_dir()


def test_relative_root_is_made_absolute_at_construction(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = LocalBlobStore(Path("relative-blobs"))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    assert store.root == tmp_path / "relative-blobs"


async def test_put_is_idempotent_and_does_not_rewrite_an_existing_blob(store, root):
    key, data = make_blob(b"idempotent")
    assert await store.put(key, data) is True
    path = blob_path(root, key)
    before = path.stat()

    assert await store.put(key, data) is False

    after = path.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert files_under(root) == [path]
    assert await store.get(key) == data


async def test_put_rejects_data_that_does_not_hash_to_the_key(store, root):
    key, _ = make_blob(b"declared content")

    with pytest.raises(BlobIntegrityError):
        await store.put(key, b"different content")

    assert files_under(root) == []
    assert not root.exists()
    assert await store.exists(key) is False


async def test_put_rejects_a_key_with_a_single_flipped_character(store, root):
    key, data = make_blob(b"payload")
    flipped = ("0" if key[0] != "0" else "1") + key[1:]

    with pytest.raises(BlobIntegrityError):
        await store.put(flipped, data)

    assert not root.exists()


async def test_hash_mismatch_is_rejected_even_when_the_blob_already_exists(store):
    key, data = make_blob(b"stored content")
    await store.put(key, data)

    with pytest.raises(BlobIntegrityError):
        await store.put(key, b"tampered content")

    assert await store.get(key) == data


def test_blob_errors_share_a_common_base():
    assert issubclass(BlobNotFound, BlobStoreError)
    assert issubclass(InvalidBlobKey, BlobStoreError)
    assert issubclass(BlobIntegrityError, BlobStoreError)
    assert not issubclass(BlobStoreError, ValueError)


@pytest.mark.parametrize("operation", list(OPERATIONS))
@pytest.mark.parametrize("key", list(BAD_KEYS.values()), ids=list(BAD_KEYS))
async def test_every_operation_rejects_malformed_keys_without_touching_the_disk(
    store, root, tmp_path, operation, key
):
    listing_before = sorted(tmp_path.iterdir())

    with pytest.raises(InvalidBlobKey):
        await OPERATIONS[operation](store, key)

    assert sorted(tmp_path.iterdir()) == listing_before
    assert not root.exists()


@pytest.mark.parametrize("key", list(BAD_KEYS.values()), ids=list(BAD_KEYS))
def test_stream_rejects_malformed_keys_before_returning_an_iterator(store, root, key):
    with pytest.raises(InvalidBlobKey):
        store.stream(key)

    assert not root.exists()


@pytest.mark.parametrize("key", ["0" * 64, "f" * 64, "0123456789abcdef" * 4])
def test_validate_blob_key_accepts_lowercase_sha256_hex(key):
    assert validate_blob_key(key) == key


def test_validate_blob_key_does_not_echo_unbounded_input():
    with pytest.raises(InvalidBlobKey) as caught:
        validate_blob_key("x" * 10_000)

    assert len(str(caught.value)) < 200


async def test_traversal_key_cannot_reach_files_outside_the_root(tmp_path):
    root = tmp_path / "deep" / "store"
    root.mkdir(parents=True)
    secret = tmp_path / ("n" * 61 + ".zip")
    secret.write_bytes(b"top secret")
    store = LocalBlobStore(root)
    key = "../" + "n" * 61
    assert len(key) == 64
    assert (root / key[:2] / f"{key}.zip").read_bytes() == b"top secret"

    for operation in OPERATIONS.values():
        with pytest.raises(InvalidBlobKey):
            await operation(store, key)
    with pytest.raises(InvalidBlobKey):
        store.stream(key)

    assert secret.read_bytes() == b"top secret"
    assert files_under(root) == []


async def test_failure_while_syncing_the_temporary_file_leaves_nothing_behind(
    store, root, monkeypatch
):
    key, data = make_blob(b"payload that never lands")

    def failing_fsync(descriptor):
        raise OSError(errno.EIO, "simulated I/O failure")

    monkeypatch.setattr(os, "fsync", failing_fsync)
    with pytest.raises(BlobStoreError) as caught:
        await store.put(key, data)
    monkeypatch.undo()

    assert isinstance(caught.value.__cause__, OSError)
    assert not isinstance(caught.value, BlobNotFound)
    assert files_under(root) == []
    assert await store.exists(key) is False
    assert await store.put(key, data) is True
    assert await store.get(key) == data


async def test_failure_at_the_final_rename_leaves_nothing_behind(store, root, monkeypatch):
    key, data = make_blob(b"payload that never lands either")
    real_replace = os.replace

    def failing_replace(source, destination, *args, **kwargs):
        if str(destination).startswith(str(root)):
            raise OSError(errno.ENOSPC, "simulated disk full")
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(BlobStoreError):
        await store.put(key, data)
    monkeypatch.undo()

    assert files_under(root) == []
    assert await store.exists(key) is False
    assert await store.put(key, data) is True


async def test_blob_is_published_by_one_rename_of_a_complete_temporary_file(
    store, root, monkeypatch
):
    key, data = make_blob(os.urandom(300_000))
    real_replace = os.replace
    observed = []

    def spying_replace(source, destination, *args, **kwargs):
        if str(destination).startswith(str(root)):
            observed.append(
                {
                    "destination_present": os.path.exists(destination),
                    "temporary_content": Path(source).read_bytes(),
                    "same_directory": os.path.dirname(source) == os.path.dirname(destination),
                }
            )
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "replace", spying_replace)
    assert await store.put(key, data) is True

    assert observed == [
        {"destination_present": False, "temporary_content": data, "same_directory": True}
    ]


async def test_put_syncs_the_file_then_renames_it_then_syncs_the_directory(store, io_events):
    key, data = make_blob(b"durable payload")

    assert await store.put(key, data) is True

    assert io_events == ["fsync-file", "rename", "fsync-directory"]


async def test_put_of_an_existing_blob_performs_no_writes(store, io_events):
    key, data = make_blob(b"already stored")
    await store.put(key, data)
    io_events.clear()

    assert await store.put(key, data) is False

    assert io_events == []


async def test_stale_temporary_file_from_a_crashed_put_does_not_block_a_new_put(store, root):
    key, data = make_blob(b"recovered after a crash")
    shard = root / key[:2]
    shard.mkdir(parents=True)
    (shard / f".{key[:16]}.crashed.tmp").write_bytes(b"partial")

    assert await store.put(key, data) is True

    assert await store.get(key) == data


async def test_io_failures_are_reported_as_blob_store_errors(tmp_path):
    root = tmp_path / "not-a-directory"
    root.write_bytes(b"i am a file")
    store = LocalBlobStore(root)
    key, data = make_blob(b"cannot be stored")

    with pytest.raises(BlobStoreError) as put_error:
        await store.put(key, data)
    with pytest.raises(BlobStoreError) as get_error:
        await store.get(key)
    with pytest.raises(BlobStoreError) as open_error:
        await store.open(key)

    assert not isinstance(put_error.value, BlobNotFound)
    assert not isinstance(get_error.value, BlobNotFound)
    assert not isinstance(open_error.value, BlobNotFound)
    assert root.read_bytes() == b"i am a file"


async def test_reading_a_path_that_is_a_directory_is_a_store_error_not_a_missing_blob(store, root):
    key, _ = make_blob(b"directory in the way")
    blob_path(root, key).mkdir(parents=True)

    with pytest.raises(BlobStoreError) as caught:
        await store.get(key)

    assert not isinstance(caught.value, BlobNotFound)


async def test_delete_removes_only_the_requested_blob(store, root):
    key_a, data_a = make_blob(b"blob a")
    key_b, data_b = make_blob(b"blob b")
    await store.put(key_a, data_a)
    await store.put(key_b, data_b)

    assert await store.delete(key_a) is True

    assert await store.exists(key_a) is False
    assert not blob_path(root, key_a).exists()
    with pytest.raises(BlobNotFound):
        await store.get(key_a)
    assert await store.get(key_b) == data_b


async def test_delete_is_idempotent_and_the_blob_can_be_stored_again(store):
    key, data = make_blob(b"deleted twice")
    await store.put(key, data)

    assert await store.delete(key) is True
    assert await store.delete(key) is False
    assert await store.put(key, data) is True
    assert await store.get(key) == data


async def test_stream_yields_fixed_size_chunks_and_a_shorter_last_one(store):
    key, data = make_blob(os.urandom(10_000))
    await store.put(key, data)

    chunks = await collect(store.stream(key, chunk_size=4096))

    assert [len(chunk) for chunk in chunks] == [4096, 4096, 1808]
    assert b"".join(chunks) == data


async def test_stream_of_an_exact_multiple_has_no_trailing_empty_chunk(store):
    key, data = make_blob(os.urandom(8192))
    await store.put(key, data)

    chunks = await collect(store.stream(key, chunk_size=4096))

    assert [len(chunk) for chunk in chunks] == [4096, 4096]


async def test_stream_uses_the_default_chunk_size(store):
    key, data = make_blob(os.urandom(DEFAULT_CHUNK_SIZE * 3 + 1))
    await store.put(key, data)

    chunks = await collect(store.stream(key))

    assert [len(chunk) for chunk in chunks] == [DEFAULT_CHUNK_SIZE] * 3 + [1]
    assert b"".join(chunks) == data


@pytest.mark.parametrize("chunk_size", [0, -1])
def test_stream_rejects_non_positive_chunk_sizes_immediately(store, chunk_size):
    key, _ = make_blob(b"anything")

    with pytest.raises(ValueError):
        store.stream(key, chunk_size=chunk_size)


async def test_stream_of_a_missing_blob_fails_on_first_iteration(store):
    key, _ = make_blob(b"missing for stream")
    iterator = store.stream(key)

    with pytest.raises(BlobNotFound):
        await anext(iterator)


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="needs /proc/self/fd")
async def test_closing_a_partially_consumed_stream_releases_the_file_descriptor(store):
    key, data = make_blob(os.urandom(50_000))
    await store.put(key, data)
    before = len(os.listdir("/proc/self/fd"))

    iterator = store.stream(key, chunk_size=1000)
    assert len(await anext(iterator)) == 1000
    during = len(os.listdir("/proc/self/fd"))
    await iterator.aclose()

    assert during > before
    assert len(os.listdir("/proc/self/fd")) == before


async def test_stream_closes_its_file_after_full_consumption(store, opened_handles):
    key, data = make_blob(os.urandom(5000))
    await store.put(key, data)

    await collect(store.stream(key, chunk_size=1000))

    assert len(opened_handles) == 1
    assert opened_handles[0].closed


async def test_stream_closes_its_file_when_the_consumer_stops_early(store, opened_handles):
    key, data = make_blob(os.urandom(5000))
    await store.put(key, data)

    iterator = store.stream(key, chunk_size=1000)
    await anext(iterator)
    assert len(opened_handles) == 1
    assert not opened_handles[0].closed
    await iterator.aclose()

    assert opened_handles[0].closed


async def test_stream_read_failure_is_a_store_error_and_closes_the_file(store, monkeypatch):
    key, data = make_blob(os.urandom(5000))
    await store.put(key, data)
    readers = []
    real_open = Path.open

    def failing_open(self, *args, **kwargs):
        reader = FailingSecondRead(real_open(self, *args, **kwargs))
        readers.append(reader)
        return reader

    monkeypatch.setattr(Path, "open", failing_open)
    iterator = store.stream(key, chunk_size=1000)
    assert len(await anext(iterator)) == 1000

    with pytest.raises(BlobStoreError) as caught:
        await anext(iterator)

    assert not isinstance(caught.value, BlobNotFound)
    assert len(readers) == 1
    assert readers[0].closed


async def test_open_returns_a_seekable_read_only_handle_with_the_exact_bytes(store):
    key, data = make_blob(os.urandom(20_000))
    await store.put(key, data)

    handle = await store.open(key)
    try:
        assert handle.seekable() and handle.readable() and not handle.writable()
        assert handle.read() == data
        handle.seek(1234)
        assert handle.read(100) == data[1234:1334]
        assert handle.seek(-10, os.SEEK_END) == len(data) - 10
        assert handle.read() == data[-10:]
        assert handle.seek(0, os.SEEK_END) == len(data)
        with pytest.raises(io.UnsupportedOperation):
            handle.write(b"x")
    finally:
        handle.close()


async def test_open_does_not_load_the_blob_into_memory(store, monkeypatch):
    key, data = make_blob(os.urandom(3_000_000))
    await store.put(key, data)

    def forbidden(self):
        raise AssertionError("the whole blob was read")

    monkeypatch.setattr(Path, "read_bytes", forbidden)
    handle = await store.open(key)
    try:
        assert handle.tell() == 0
        assert handle.read(10) == data[:10]
        assert handle.tell() == 10
    finally:
        handle.close()


async def test_every_open_returns_an_independent_handle(store):
    key, data = make_blob(os.urandom(5000))
    await store.put(key, data)

    first, second = await store.open(key), await store.open(key)
    try:
        assert first is not second
        first.seek(4000)
        assert second.read(10) == data[:10]
        assert first.read(10) == data[4000:4010]
        first.close()
        assert first.closed and not second.closed
        assert second.read(10) == data[10:20]
    finally:
        first.close()
        second.close()


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="needs /proc/self/fd")
async def test_the_caller_owns_the_handle_and_closing_it_releases_the_descriptor(store):
    key, data = make_blob(os.urandom(5000))
    await store.put(key, data)
    before = len(os.listdir("/proc/self/fd"))

    handle = await store.open(key)
    during = len(os.listdir("/proc/self/fd"))
    assert not handle.closed
    handle.close()

    assert during == before + 1
    assert len(os.listdir("/proc/self/fd")) == before


async def test_open_of_a_missing_blob_is_blob_not_found_and_creates_nothing(store, root):
    key, _ = make_blob(b"never stored for open")

    with pytest.raises(BlobNotFound):
        await store.open(key)

    assert not root.exists()


async def test_open_of_a_directory_is_a_store_error_not_a_missing_blob(store, root):
    key, _ = make_blob(b"directory in the way of open")
    blob_path(root, key).mkdir(parents=True)

    with pytest.raises(BlobStoreError) as caught:
        await store.open(key)

    assert not isinstance(caught.value, BlobNotFound)


async def test_open_reports_permission_errors_as_store_errors(store, monkeypatch):
    key, data = make_blob(b"unreadable")
    await store.put(key, data)

    def denied(self, *args, **kwargs):
        raise PermissionError(errno.EACCES, "simulated denial")

    monkeypatch.setattr(Path, "open", denied)
    with pytest.raises(BlobStoreError) as caught:
        await store.open(key)

    assert not isinstance(caught.value, BlobNotFound)
    assert isinstance(caught.value.__cause__, PermissionError)


async def test_event_loop_stays_responsive_during_a_slow_open(store, monkeypatch):
    key, data = make_blob(b"slow open")
    await store.put(key, data)
    real_open = Path.open

    def slow_open(self, *args, **kwargs):
        time.sleep(0.3)
        return real_open(self, *args, **kwargs)

    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    monkeypatch.setattr(Path, "open", slow_open)
    task = asyncio.create_task(ticker())
    try:
        handle = await store.open(key)
        handle.close()
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    assert ticks >= 5


async def test_two_concurrent_identical_puts_create_the_blob_exactly_once(store, root, monkeypatch):
    key, data = make_blob(os.urandom(100_000))
    barrier = threading.Barrier(2, timeout=10)
    real_fsync = os.fsync

    def synchronized_fsync(descriptor):
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            barrier.wait()
        return real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", synchronized_fsync)
    results = await asyncio.gather(store.put(key, data), store.put(key, data))

    assert sorted(results) == [False, True]
    assert await store.get(key) == data
    assert files_under(root) == [blob_path(root, key)]


async def test_many_concurrent_identical_puts_report_a_single_creation(store, root):
    key, data = make_blob(os.urandom(200_000))

    results = await asyncio.gather(*(store.put(key, data) for _ in range(12)))

    assert results.count(True) == 1
    assert results.count(False) == 11
    assert await store.get(key) == data
    assert files_under(root) == [blob_path(root, key)]


async def test_concurrent_puts_of_different_blobs_all_succeed(store):
    blobs = [make_blob(os.urandom(1000 + index * 37)) for index in range(16)]

    results = await asyncio.gather(*(store.put(key, data) for key, data in blobs))

    assert results == [True] * 16
    for key, data in blobs:
        assert await store.get(key) == data


async def test_event_loop_stays_responsive_during_a_slow_write(store, monkeypatch):
    key, data = make_blob(b"slow write")
    real_fsync = os.fsync

    def slow_fsync(descriptor):
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            time.sleep(0.3)
        return real_fsync(descriptor)

    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    monkeypatch.setattr(os, "fsync", slow_fsync)
    task = asyncio.create_task(ticker())
    try:
        await store.put(key, data)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    assert ticks >= 5


async def test_event_loop_stays_responsive_during_a_slow_read(store, monkeypatch):
    key, data = make_blob(b"slow read")
    await store.put(key, data)
    real_read_bytes = Path.read_bytes

    def slow_read_bytes(self):
        time.sleep(0.3)
        return real_read_bytes(self)

    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    monkeypatch.setattr(Path, "read_bytes", slow_read_bytes)
    task = asyncio.create_task(ticker())
    try:
        assert await store.get(key) == data
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    assert ticks >= 5


def test_local_store_satisfies_the_blob_store_protocol(store):
    assert isinstance(store, BlobStore)
    assert not isinstance(object(), BlobStore)


def test_a_store_without_random_access_reads_is_not_a_blob_store():
    class StreamOnlyStore:
        async def put(self, key, data): ...

        async def get(self, key): ...

        async def exists(self, key): ...

        async def delete(self, key): ...

        async def size(self, key): ...

        def stream(self, key, chunk_size=0): ...

    assert not isinstance(StreamOnlyStore(), BlobStore)


def test_storage_package_does_not_import_services_or_the_web_framework():
    package_dir = Path(app.storage.__file__).parent
    forbidden = ("app.services", "fastapi", "starlette")
    offenders = []

    for source in sorted(package_dir.glob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                continue
            offenders.extend(
                f"{source.name}: {module}" for module in modules if module.startswith(forbidden)
            )

    assert offenders == []


def test_get_blob_store_builds_a_local_store_from_settings(configured_root):
    store = get_blob_store()

    assert isinstance(store, LocalBlobStore)
    assert isinstance(store, BlobStore)
    assert store.root == configured_root


def test_get_blob_store_is_cached_until_the_cache_is_reset(configured_root, tmp_path, monkeypatch):
    first = get_blob_store()
    assert get_blob_store() is first

    other_root = tmp_path / "another-root"
    monkeypatch.setattr(settings.storage, "path", str(other_root))
    assert get_blob_store() is first

    reset_blob_store_cache()
    second = get_blob_store()

    assert second is not first
    assert second.root == other_root


def test_get_blob_store_does_not_create_the_directory(configured_root):
    get_blob_store()

    assert not configured_root.exists()


async def test_store_from_settings_writes_under_the_configured_directory(configured_root):
    key, data = make_blob(b"through the factory")

    assert await get_blob_store().put(key, data) is True

    assert blob_path(configured_root, key).read_bytes() == data
