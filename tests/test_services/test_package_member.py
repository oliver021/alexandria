from __future__ import annotations

import hashlib
import io
import os
import random
import threading
import tracemalloc
import zipfile
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO

import pytest

from app.services import package_inspector as inspector
from app.services.errors import (
    DomainError,
    InvalidPackageError,
    NotFoundError,
    StorageError,
    ValidationError,
)
from app.services.package_inspector import (
    PackageMember,
    inspect_package,
    open_package_member,
    read_package_file,
)
from tests.helpers.handles import CountingHandle, FlakyHandle
from tests.helpers.packages import (
    UNIX_SYMLINK,
    build_raw_zip,
    corrupt_entry_data,
    forge_compressed_size,
    forge_declared_size,
    make_macos_zip,
    make_raw_package,
    make_skill_zip,
    make_stream_zip,
    make_zeros_zip,
    make_zip,
    make_zip64_zip,
    make_zipalign_zip,
    set_end_fields,
    set_local_header_offset,
    skill_md,
    stored_entry,
    unicode_path_extra,
    zero_bomb_entry,
)

KIB = 1024
MIB = 1024 * KIB
GIB = 1024 * MIB
CHUNK = 64 * KIB

FILES = {
    "scripts/run.py": "print('hello')\n",
    "docs/guide.md": "# Guide\n\n" + "line of text\n" * 3000,
    "data.bin": bytes(range(256)) * 300,
    "empty.txt": "",
    "notes/ünïcode.txt": "héllo wörld\n",
}


def skill_files() -> dict[str, str | bytes]:
    return {"SKILL.md": skill_md("reader", "1.0.0"), **FILES}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@contextmanager
def on_disk(tmp_path: Path, blob: bytes) -> Iterator[BinaryIO]:
    target = tmp_path / "blob.zip"
    target.write_bytes(blob)
    with target.open("rb") as handle:
        yield handle


def bytes_outcome(
    blob: bytes, prefix: str, path: str, *, max_bytes: int | None = None, truncate: bool = False
) -> tuple[Any, Any]:
    try:
        return "ok", read_package_file(blob, prefix, path, max_bytes=max_bytes, truncate=truncate)
    except DomainError as error:
        return type(error), error.reason


def stream_outcome(
    blob: bytes, prefix: str, path: str, *, max_bytes: int | None = None, truncate: bool = False
) -> tuple[Any, Any]:
    try:
        with open_package_member(io.BytesIO(blob), prefix, path) as member:
            return "ok", member.read(max_bytes, truncate=truncate)
    except DomainError as error:
        return type(error), error.reason


def chunks_outcome(blob: bytes, prefix: str, path: str) -> tuple[Any, Any]:
    try:
        with open_package_member(io.BytesIO(blob), prefix, path) as member:
            return "ok", b"".join(member.chunks())
    except DomainError as error:
        return type(error), error.reason


def healthy_packages() -> dict[str, tuple[bytes, str]]:
    stream_files = skill_files()
    return {
        "flat": (make_skill_zip("reader", "1.0.0", files=FILES), ""),
        "wrapped": (
            make_skill_zip("reader", "1.0.0", files=FILES, wrapper="reader-pkg"),
            "reader-pkg/",
        ),
        "stored": (
            make_skill_zip("reader", "1.0.0", files=FILES, compression=zipfile.ZIP_STORED),
            "",
        ),
        "directories-and-junk": (
            make_skill_zip("reader", "1.0.0", files=FILES, dir_entries=True, macos_junk=True),
            "",
        ),
        "data-descriptors": (make_stream_zip(stream_files), ""),
        "data-descriptors-stored": (
            make_stream_zip(stream_files, compression=zipfile.ZIP_STORED),
            "",
        ),
        "zip64": (make_zip64_zip(stream_files), ""),
        "zip64-stored": (make_zip64_zip(stream_files, compression=zipfile.ZIP_STORED), ""),
        "zipalign": (make_zipalign_zip(stream_files, alignment=4), ""),
        "macos": (make_macos_zip(stream_files, wrapper="package"), "package/"),
        "commented": (make_stream_zip(stream_files, comment=b"a zip comment"), ""),
    }


def hostile_cases() -> dict[str, tuple[bytes, str, str, dict[str, Any]]]:
    plain = make_skill_zip(
        "reader", "1.0.0", files={"a.txt": "x" * 3000, "b.bin": bytes(range(200))}
    )
    stored = make_skill_zip(
        "reader", "1.0.0", files={"a.bin": bytes(range(200))}, compression=zipfile.ZIP_STORED
    )
    nested = make_skill_zip("reader", "1.0.0", files={"docs/a.txt": "x"}, dir_entries=True)
    junk = make_skill_zip(
        "reader", "1.0.0", files={"keep.txt": "k"}, wrapper="pkg", macos_junk=True
    )
    bomb = make_raw_package([zero_bomb_entry("z.bin", 3 * GIB, declared_size=2000)])
    big_bomb = make_raw_package([zero_bomb_entry("z.bin", 3 * GIB, declared_size=3 * GIB - 1)])
    cut = len(plain) // 2
    cases: dict[str, tuple[bytes, str, str, dict[str, Any]]] = {
        "symlink": (
            make_raw_package([stored_entry("link", "target", external_attr=UNIX_SYMLINK)]),
            "",
            "link",
            {},
        ),
        "encrypted": (
            make_raw_package([stored_entry("locked", b"\x01" * 10, flags=0x1)]),
            "",
            "locked",
            {},
        ),
        "exotic-compression": (
            make_raw_package([stored_entry("exotic", b"x", method=12)]),
            "",
            "exotic",
            {},
        ),
        "duplicate-entries": (
            make_zip(
                [("SKILL.md", skill_md("dup", "1.0.0")), ("a.txt", "first"), ("a.txt", "second")]
            ),
            "",
            "a.txt",
            {},
        ),
        "conflicting-names": (
            build_raw_zip(
                [stored_entry("safe.txt", "x", extra=unicode_path_extra("safe.txt", "other.txt"))]
            ),
            "",
            "safe.txt",
            {},
        ),
        "not-a-zip": (b"this is not a zip", "", "SKILL.md", {}),
        "empty-blob": (b"", "", "SKILL.md", {}),
        "magic-only": (b"PK\x03\x04", "", "SKILL.md", {}),
        "end-record-only": (b"PK\x05\x06" + bytes(18), "", "SKILL.md", {}),
        "flipped-stored-byte": (corrupt_entry_data(stored, "a.bin", index=10), "", "a.bin", {}),
        "flipped-deflate-byte": (corrupt_entry_data(plain, "b.bin", index=3), "", "b.bin", {}),
        "declared-size-too-small": (forge_declared_size(plain, "a.txt", 10), "", "a.txt", {}),
        "declared-size-too-big": (forge_declared_size(plain, "a.txt", 90_000), "", "a.txt", {}),
        "compressed-size-too-small": (forge_compressed_size(plain, "a.txt", 5), "", "a.txt", {}),
        "compressed-size-too-big": (
            forge_compressed_size(plain, "a.txt", 900_000),
            "",
            "a.txt",
            {},
        ),
        "local-header-moved": (set_local_header_offset(plain, "a.txt", 7), "", "a.txt", {}),
        "local-header-outside": (
            set_local_header_offset(plain, "a.txt", len(plain) + 99),
            "",
            "a.txt",
            {},
        ),
        "truncated-in-the-middle": (plain[:cut], "", "a.txt", {}),
        "missing-last-byte": (plain[:-1], "", "a.txt", {}),
        "central-directory-beyond-the-file": (
            set_end_fields(plain, size=1 << 30),
            "",
            "a.txt",
            {},
        ),
        "wrong-central-directory-offset": (set_end_fields(plain, offset=3), "", "a.txt", {}),
        "more-entries-than-declared": (
            set_end_fields(plain, entries_here=1, entries=1),
            "",
            "a.txt",
            {},
        ),
        "bomb-without-a-cap": (bomb, "", "z.bin", {}),
        "bomb-with-a-cap": (big_bomb, "", "z.bin", {"max_bytes": 1 * MIB}),
        "bomb-truncated": (big_bomb, "", "z.bin", {"max_bytes": 100, "truncate": True}),
        "missing-file": (plain, "", "nope.txt", {}),
        "a-directory": (nested, "", "docs", {}),
        "wrong-root-prefix": (plain, "other/", "a.txt", {}),
        "ignored-ds-store": (junk, "pkg/", ".DS_Store", {}),
        "ignored-macosx": (junk, "", "__MACOSX/pkg/._SKILL.md", {}),
        "cap-below-size": (plain, "", "a.txt", {"max_bytes": 100}),
        "cap-below-size-truncated": (plain, "", "a.txt", {"max_bytes": 100, "truncate": True}),
        "cap-of-zero": (plain, "", "a.txt", {"max_bytes": 0}),
        "negative-cap": (plain, "", "a.txt", {"max_bytes": -1}),
        "invalid-root-prefix": (plain, "../", "a.txt", {}),
        "invalid-root-prefix-no-slash": (plain, "pkg", "a.txt", {}),
        "invalid-path-traversal": (plain, "", "../a.txt", {}),
        "invalid-path-absolute": (plain, "", "/a.txt", {}),
        "invalid-path-backslash": (plain, "", "a\\b", {}),
        "invalid-path-nul": (plain, "", "a\x00b", {}),
    }
    return cases


HEALTHY = healthy_packages()
HOSTILE = hostile_cases()


def manifest_paths(blob: bytes) -> list[str]:
    return [entry.path for entry in inspect_package(blob).manifest]


@pytest.mark.parametrize("name", list(HEALTHY))
def test_a_stream_reads_every_file_exactly_like_the_in_memory_reader(name):
    blob, prefix = HEALTHY[name]
    paths = manifest_paths(blob)

    assert set(FILES) <= set(paths)
    for path in paths:
        expected = read_package_file(blob, prefix, path)
        assert sha(expected) == next(
            entry.sha256 for entry in inspect_package(blob).manifest if entry.path == path
        )
        assert stream_outcome(blob, prefix, path) == ("ok", expected)
        assert chunks_outcome(blob, prefix, path) == ("ok", expected)


@pytest.mark.parametrize("name", list(HEALTHY))
def test_a_stream_on_disk_reads_every_file_exactly_like_the_in_memory_reader(name, tmp_path):
    blob, prefix = HEALTHY[name]

    with on_disk(tmp_path, blob) as handle:
        for path in manifest_paths(blob):
            with open_package_member(handle, prefix, path) as member:
                assert member.read() == read_package_file(blob, prefix, path)
            with open_package_member(handle, prefix, path) as member:
                assert b"".join(member.chunks()) == read_package_file(blob, prefix, path)


@pytest.mark.parametrize("name", list(HOSTILE))
def test_hostile_blobs_fail_through_a_stream_exactly_as_they_fail_in_memory(name):
    blob, prefix, path, options = HOSTILE[name]
    expected = bytes_outcome(blob, prefix, path, **options)

    assert stream_outcome(blob, prefix, path, **options) == expected
    if not options:
        assert chunks_outcome(blob, prefix, path) == expected


def test_the_hostile_cases_exercise_every_kind_of_failure():
    outcomes = {
        bytes_outcome(blob, prefix, path, **options)
        for blob, prefix, path, options in HOSTILE.values()
    }
    reasons = {reason for kind, reason in outcomes if kind != "ok"}

    assert {
        "NOT_A_ZIP",
        "CORRUPT_ARCHIVE",
        "ENCRYPTED_ENTRY",
        "UNSUPPORTED_COMPRESSION",
        "SYMLINK_NOT_ALLOWED",
        "UNSAFE_PATH",
        "DUPLICATE_PATH",
        "TOO_LARGE",
        "INVALID_PATH",
    } <= reasons
    assert {NotFoundError, StorageError, ValidationError, InvalidPackageError} <= {
        kind for kind, _ in outcomes
    }
    assert ("ok", bytes(100)) in outcomes


def test_a_central_directory_above_the_read_cap_is_refused_on_streams_as_in_memory(monkeypatch):
    blob = make_skill_zip("reader", "1.0.0", files=FILES)
    monkeypatch.setattr(inspector, "_READ_CENTRAL_DIRECTORY_CAP", 64)
    stream = CountingHandle(io.BytesIO(blob))

    with pytest.raises(InvalidPackageError) as caught:
        open_package_member(stream, "", "SKILL.md")

    assert caught.value.reason == "TOO_MANY_FILES"
    assert bytes_outcome(blob, "", "SKILL.md") == (InvalidPackageError, "TOO_MANY_FILES")
    assert stream_outcome(blob, "", "SKILL.md") == (InvalidPackageError, "TOO_MANY_FILES")
    assert stream.bytes_read <= inspector._END_SEARCH_BYTES + 64 * KIB


def test_chunks_are_bounded_and_add_up_to_the_member():
    content = os.urandom(300 * KIB) + bytes(300 * KIB) + os.urandom(1000)
    blob = make_skill_zip("reader", "1.0.0", files={"big.bin": content})

    with open_package_member(io.BytesIO(blob), "", "big.bin") as member:
        chunks = list(member.chunks())
        assert member.size == len(content)

    assert b"".join(chunks) == content
    assert 1 < len(chunks)
    assert all(0 < len(chunk) <= CHUNK for chunk in chunks)


@pytest.mark.parametrize("compression", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
def test_an_empty_member_yields_no_chunks(compression):
    blob = make_skill_zip("reader", "1.0.0", files={"empty.txt": ""}, compression=compression)

    with open_package_member(io.BytesIO(blob), "", "empty.txt") as member:
        assert member.size == 0
        assert list(member.chunks()) == []
        assert member.read() == b""


def test_a_member_can_be_read_again_and_chunks_can_be_restarted():
    blob = make_skill_zip("reader", "1.0.0", files={"a.txt": "alpha\n" * 5000})
    expected = ("alpha\n" * 5000).encode()

    with open_package_member(io.BytesIO(blob), "", "a.txt") as member:
        assert b"".join(member.chunks()) == expected
        assert b"".join(member.chunks()) == expected
        assert member.read() == expected
        assert member.read(100, truncate=True) == expected[:100]


def test_abandoning_the_chunks_midway_does_not_disturb_the_next_read():
    blob = make_skill_zip("reader", "1.0.0", files={"a.bin": os.urandom(400 * KIB)})

    with open_package_member(io.BytesIO(blob), "", "a.bin") as member:
        first = member.chunks()
        next(first)
        first.close()
        assert member.read() == read_package_file(blob, "", "a.bin")


def test_truncated_reads_return_exactly_the_requested_prefix_across_chunk_boundaries():
    content = bytes(range(256)) * 1000
    for compression in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        blob = make_skill_zip("reader", "1.0.0", files={"f.bin": content}, compression=compression)
        with open_package_member(io.BytesIO(blob), "", "f.bin") as member:
            for size in (
                0,
                1,
                65535,
                65536,
                65537,
                131072,
                200_000,
                len(content),
                len(content) + 9,
            ):
                assert member.read(size, truncate=True) == content[:size]


def test_a_negative_cap_is_a_validation_error():
    blob = make_skill_zip("reader", "1.0.0")

    with open_package_member(io.BytesIO(blob), "", "SKILL.md") as member:
        for truncate in (False, True):
            with pytest.raises(ValidationError, match="max_bytes"):
                member.read(-1, truncate=truncate)


def test_a_member_is_not_opened_for_an_invalid_prefix_or_path_and_the_stream_is_not_touched():
    blob = make_skill_zip("reader", "1.0.0")
    stream = CountingHandle(io.BytesIO(blob))

    with pytest.raises(StorageError):
        open_package_member(stream, "../", "SKILL.md")
    with pytest.raises(ValidationError) as caught:
        open_package_member(stream, "", "../SKILL.md")

    assert caught.value.reason == "INVALID_PATH"
    assert (stream.reads, stream.bytes_read) == (0, 0)


def test_closing_a_member_and_a_failed_open_leave_the_stream_to_its_owner():
    blob = make_skill_zip("reader", "1.0.0")
    stream = io.BytesIO(blob)

    member = open_package_member(stream, "", "SKILL.md")
    member.close()
    with pytest.raises(NotFoundError):
        open_package_member(stream, "", "missing.txt")
    with pytest.raises(InvalidPackageError):
        open_package_member(io.BytesIO(b"not a zip"), "", "SKILL.md")

    assert not stream.closed
    assert isinstance(member, PackageMember)


def test_the_context_manager_closes_the_member_even_when_the_body_fails():
    blob = make_skill_zip("reader", "1.0.0")
    stream = io.BytesIO(blob)

    with pytest.raises(RuntimeError), open_package_member(stream, "", "SKILL.md"):
        raise RuntimeError("boom")

    assert not stream.closed


def test_reading_a_small_member_touches_a_small_part_of_a_large_blob(tmp_path):
    incompressible = os.urandom(6 * MIB)
    blob = make_skill_zip(
        "reader", "1.0.0", files={"huge.bin": incompressible}, compression=zipfile.ZIP_STORED
    )
    assert len(blob) > 6 * MIB

    with on_disk(tmp_path, blob) as handle:
        counted = CountingHandle(handle)
        with open_package_member(counted, "", "SKILL.md") as member:
            assert member.read() == read_package_file(blob, "", "SKILL.md")

    assert 0 < counted.bytes_read < 256 * KIB


def test_streaming_a_stored_member_reads_it_once_and_nothing_else(tmp_path):
    content = os.urandom(3 * MIB)
    blob = make_skill_zip(
        "reader",
        "1.0.0",
        files={"a.bin": content, "b.bin": os.urandom(3 * MIB)},
        compression=zipfile.ZIP_STORED,
    )

    with on_disk(tmp_path, blob) as handle:
        counted = CountingHandle(handle)
        with open_package_member(counted, "", "a.bin") as member:
            assert b"".join(member.chunks()) == content

    assert len(content) <= counted.bytes_read < len(content) + 256 * KIB


def test_streaming_a_large_member_keeps_memory_flat(tmp_path):
    blob = make_zeros_zip(40 * MIB)
    assert len(blob) < 2 * MIB

    with on_disk(tmp_path, blob) as handle:
        tracemalloc.start()
        try:
            digest = hashlib.sha256()
            total = 0
            with open_package_member(handle, "", "payload.bin") as member:
                for chunk in member.chunks():
                    digest.update(chunk)
                    total += len(chunk)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

    assert total == 40 * MIB
    assert digest.hexdigest() == sha(bytes(40 * MIB))
    assert peak < 2 * MIB


def test_reading_the_prefix_of_a_large_member_does_not_inflate_the_rest(tmp_path):
    blob = make_zeros_zip(200 * MIB)

    with on_disk(tmp_path, blob) as handle:
        counted = CountingHandle(handle)
        tracemalloc.start()
        try:
            with open_package_member(counted, "", "payload.bin") as member:
                prefix = member.read(1000, truncate=True)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

    assert prefix == bytes(1000)
    assert peak < 1 * MIB


@pytest.mark.parametrize("compression", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
def test_io_failures_surface_as_domain_errors_never_as_os_errors(compression):
    blob = make_skill_zip(
        "reader",
        "1.0.0",
        files={"a.bin": os.urandom(150 * KIB), "z.txt": "text\n" * 20_000},
        compression=compression,
    )
    failures = set()
    successes = 0

    for fail_read in [None, *range(14)]:
        for fail_seek in [None, *range(1, 14)]:
            if fail_read is None and fail_seek is None:
                continue
            handle = FlakyHandle(io.BytesIO(blob), fail_read=fail_read, fail_seek=fail_seek)
            try:
                with open_package_member(handle, "", "a.bin") as member:
                    assert b"".join(member.chunks()) == read_package_file(blob, "", "a.bin")
                successes += 1
            except DomainError as error:
                failures.add(type(error))

    assert failures
    assert failures <= {StorageError, InvalidPackageError}
    assert successes


def test_the_file_view_slices_like_bytes_for_every_kind_of_slice():
    rng = random.Random(20260116)
    for _ in range(40):
        data = rng.randbytes(rng.randrange(0, 300))
        view = inspector._FileData(io.BytesIO(data))
        assert len(view) == len(data)
        bounds = [None, *range(-320, 320, 11)]
        for start in bounds:
            for stop in bounds:
                assert view[start:stop] == data[start:stop], (start, stop, len(data))


def test_the_file_view_refuses_strided_slices_and_maps_io_errors():
    view = inspector._FileData(io.BytesIO(b"0123456789"))

    with pytest.raises(TypeError):
        view[::2]
    with pytest.raises(StorageError):
        inspector._FileData(FlakyHandle(io.BytesIO(b"x"), fail_read=None, fail_seek=1))
    broken = FlakyHandle(io.BytesIO(b"0123456789"), fail_read=None, fail_seek=2)
    unreliable = inspector._FileData(broken)
    with pytest.raises(StorageError) as caught:
        unreliable[2:5]
    assert caught.value.message == "The stored package cannot be read"


def test_threads_can_read_different_members_of_the_same_blob_through_their_own_streams():
    blob, prefix = HEALTHY["wrapped"]
    paths = manifest_paths(blob)
    expected = {path: read_package_file(blob, prefix, path) for path in paths}
    barrier = threading.Barrier(8)
    failures: list[tuple[str, str]] = []

    def work(seed: int) -> None:
        rng = random.Random(seed)
        barrier.wait()
        for _ in range(60):
            path = rng.choice(paths)
            with open_package_member(io.BytesIO(blob), prefix, path) as member:
                if b"".join(member.chunks()) != expected[path]:
                    failures.append(("chunks", path))
                if member.read() != expected[path]:
                    failures.append(("read", path))

    with ThreadPoolExecutor(max_workers=8) as pool:
        for future in [pool.submit(work, seed) for seed in range(8)]:
            future.result()

    assert failures == []


def mutate(rng: random.Random, blob: bytes) -> bytes:
    data = bytearray(blob)
    for _ in range(rng.choice((1, 1, 2, 3, 8))):
        if not data:
            data = bytearray(b"x")
        position = rng.randrange(len(data))
        roll = rng.random()
        if roll < 0.5:
            data[position] ^= 1 << rng.randrange(8)
        elif roll < 0.75:
            data[position] = rng.randrange(256)
        elif roll < 0.85:
            del data[position : position + rng.randrange(1, 40)]
        elif roll < 0.95:
            data[position:position] = bytes(rng.randrange(256) for _ in range(rng.randrange(1, 20)))
        else:
            data = data[: max(1, position)]
    return bytes(data)


def test_random_corruption_makes_a_stream_and_the_in_memory_reader_agree():
    bases = [
        (
            make_skill_zip(
                "fuzz-a",
                "1.0.0",
                files={"scripts/run.py": "print(1)\n" * 20, "data.bin": bytes(range(256)) * 4},
                dir_entries=True,
                macos_junk=True,
                wrapper="pkg",
            ),
            "pkg/",
        ),
        (
            make_skill_zip(
                "fuzz-b", "2.0.0-rc.1", files={"a.txt": "hello", "b/c.txt": "world" * 100}
            ),
            "",
        ),
        (
            make_skill_zip(
                "fuzz-c",
                "0.1.0",
                files={"data.bin": bytes(range(256)) * 8},
                compression=zipfile.ZIP_STORED,
            ),
            "",
        ),
        (make_stream_zip(skill_files(), comment=b"fuzz"), ""),
        (make_zip64_zip(skill_files()), ""),
    ]
    targets = ["SKILL.md", "scripts/run.py", "data.bin", "a.txt", "b/c.txt", "docs/guide.md"]
    rng = random.Random(20260117)
    outcomes: set[Any] = set()

    for _ in range(2500):
        base, prefix = rng.choice(bases)
        blob = mutate(rng, base)
        path = rng.choice(targets)
        expected = bytes_outcome(blob, prefix, path)
        assert stream_outcome(blob, prefix, path) == expected, (blob, prefix, path)
        assert chunks_outcome(blob, prefix, path) == expected, (blob, prefix, path)
        outcomes.add(expected[0] if expected[0] == "ok" else expected[1])

    assert "ok" in outcomes
    assert {"CORRUPT_ARCHIVE", "NOT_A_ZIP"} <= outcomes


def test_the_reader_of_a_valid_blob_accepts_the_ignored_junk_next_to_the_files():
    blob = make_skill_zip(
        "reader", "1.0.0", files={"keep.txt": "k"}, wrapper="pkg", macos_junk=True
    )
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        assert any("__MACOSX" in name for name in archive.namelist())

    with open_package_member(io.BytesIO(blob), "pkg/", "keep.txt") as member:
        assert member.read() == b"k"


def test_the_stream_api_never_leaks_a_bare_value_error():
    blob = make_skill_zip("reader", "1.0.0")
    hostile = [
        ("", "SKILL.md\x00"),
        ("a/b/", "SKILL.md"),
        ("", ""),
        ("", "."),
        ("", "nope.txt"),
        ("x\\", "SKILL.md"),
    ]
    for prefix, path in hostile:
        with pytest.raises(DomainError) as caught:
            open_package_member(io.BytesIO(blob), prefix, path)
        assert type(caught.value) is not ValueError


def test_the_stream_api_is_part_of_the_public_surface():
    assert inspector.PackageMember is PackageMember
    assert {"PackageMember", "open_package_member"} <= set(inspector.__all__)
