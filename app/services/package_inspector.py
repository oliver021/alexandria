from __future__ import annotations

import codecs
import copy
import hashlib
import io
import json
import math
import mimetypes
import re
import stat
import struct
import threading
import unicodedata
import uuid
import zipfile
import zlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date, datetime
from operator import attrgetter
from typing import Any, BinaryIO, Literal, NamedTuple, Self

import yaml

from app.services import semver
from app.services.errors import InvalidPackageError, NotFoundError, StorageError, ValidationError

__all__ = [
    "DEFAULT_LIMITS",
    "ENTRYPOINT_KINDS",
    "GROUNDING_ENTRYPOINT",
    "SKILL_ENTRYPOINT",
    "Kind",
    "ManifestEntry",
    "PackageLimits",
    "PackageMember",
    "ParsedPackage",
    "guess_media_type",
    "inspect_package",
    "is_text_sample",
    "looks_like_uuid",
    "open_package_member",
    "read_package_file",
    "split_frontmatter",
    "validate_requested_path",
]

Kind = Literal["skill", "grounding"]

SKILL_ENTRYPOINT = "SKILL.md"
GROUNDING_ENTRYPOINT = "INDEX.md"
ENTRYPOINT_KINDS: dict[str, Kind] = {
    SKILL_ENTRYPOINT: "skill",
    GROUNDING_ENTRYPOINT: "grounding",
}

FRONTMATTER_MAX_BYTES = 16 * 1024
_ENTRYPOINT_LABEL = f"{SKILL_ENTRYPOINT} or {GROUNDING_ENTRYPOINT}"
SEARCH_TEXT_MAX_CHARS = 20_000
TEXT_SAMPLE_BYTES = 8 * 1024
NAME_MAX_LENGTH = 64
DESCRIPTION_MAX_LENGTH = 1024
DISPLAY_NAME_MAX_LENGTH = 255
TAGS_MAX_COUNT = 20

_CHUNK_BYTES = 64 * 1024
_UNBOUNDED_SIZE = 1 << 62
_ENTRYPOINT_TEXT_CHARS = 256 * 1024
_ENTRY_ALLOWANCE_FACTOR = 4
_ENTRY_ALLOWANCE_SLACK = 64
_CENTRAL_DIRECTORY_BYTES_PER_ENTRY = 300
_MIN_CENTRAL_DIRECTORY_CAP = 1 << 20
_END_SIGNATURE = b"PK\x05\x06"
_ZIP_MAGIC_PREFIXES = (b"PK\x03\x04", _END_SIGNATURE, b"PK\x07\x08")
_END_SEARCH_BYTES = 22 + 65535
_READ_CENTRAL_DIRECTORY_CAP = 16 << 20
_QUOTE_LENGTH = 60
_MAX_FLOW_DEPTH = 32
_UNREAD_STREAM_BYTES = 64 * 1024 * 1024

_IGNORED_ROOT_DIRECTORY = "__MACOSX"
_IGNORED_FILE_NAMES = frozenset({".DS_Store", "Thumbs.db"})
_SUPPORTED_COMPRESSION = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})
_ENCRYPTION_FLAGS = 0x1 | 0x40
_DESCRIPTOR_FLAG = 0x8
_UTF8_NAME_FLAG = 0x800
_LOCAL_LAYOUT_FLAGS = _ENCRYPTION_FLAGS | _DESCRIPTOR_FLAG
_LOCAL_HEADER = struct.Struct("<4sHHHHHIIIHH")
_LOCAL_SIGNATURE = b"PK\x03\x04"
_EXACT_EXTENTS = frozenset({0})
_DESCRIPTOR_EXTENTS = frozenset({0, 12, 16, 20, 24})
_SIGNED_DESCRIPTOR_EXTENTS = frozenset({16, 24})
_WIDE_DESCRIPTOR_EXTENTS = frozenset({20, 24})
_DESCRIPTOR_SIGNATURE = b"PK\x07\x08"
_NARROW_DESCRIPTOR = struct.Struct("<III")
_WIDE_DESCRIPTOR = struct.Struct("<IQQ")
_ZIP64_MARKER = 0xFFFFFFFF
_ZIP64_EXTRA_ID = 0x0001
_EXTRA_HEADER = struct.Struct("<HH")
_ZIP64_SIZES = struct.Struct("<QQ")
_MAX_EXTRA_RECORDS = 64
_PATCHED_DATA_FLAG = 0x20
_CENTRAL_SIGNATURE = b"PK\x01\x02"
_CENTRAL_HEADER_SIZE = 46
_CENTRAL_LENGTHS = struct.Struct("<HHH")
_CENTRAL_LENGTHS_OFFSET = 28
_END_RECORD = struct.Struct("<4sHHHHIIH")
_ZIP64_LOCATOR = struct.Struct("<4sIQI")
_ZIP64_LOCATOR_SIGNATURE = b"PK\x06\x07"
_ZIP64_END_RECORD = struct.Struct("<4sQHHIIQQQQ")
_ZIP64_END_SIGNATURE = b"PK\x06\x06"
_END_MARKERS = (0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF, _ZIP64_MARKER, _ZIP64_MARKER)


def _character_class(*ranges: tuple[int, int]) -> re.Pattern[str]:
    members = "".join(f"{re.escape(chr(low))}-{re.escape(chr(high))}" for low, high in ranges)
    return re.compile(f"[{members}]")


_NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_TAG_PATTERN = re.compile(r"[a-z0-9][a-z0-9._+-]{0,31}")
_CATEGORY_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_DRIVE_PREFIX = re.compile(r"[A-Za-z]:")
_UNSAFE_PATH_CHARACTERS = _character_class(
    (0x00, 0x1F), (0x7F, 0x9F), (0x202A, 0x202E), (0x2066, 0x2069), (0xD800, 0xDFFF)
)
_CONTROL_TEXT = _character_class(
    (0x00, 0x08),
    (0x0B, 0x0C),
    (0x0E, 0x1F),
    (0x7F, 0x84),
    (0x86, 0x9F),
    (0xD800, 0xDFFF),
    (0xFFFE, 0xFFFF),
)
_OPENING_DELIMITER = re.compile(r"---[ \t]*\r?\n")
_CLOSING_DELIMITER = re.compile(r"^---[ \t]*(?:\r?\n|\Z)", re.MULTILINE)

_ARCHIVE_ERRORS = (
    zipfile.BadZipFile,
    zipfile.LargeZipFile,
    UnicodeDecodeError,
    struct.error,
    NotImplementedError,
    OverflowError,
    EOFError,
    ValueError,
    OSError,
)
_READ_ERRORS = (
    zipfile.BadZipFile,
    zlib.error,
    struct.error,
    EOFError,
    NotImplementedError,
    RuntimeError,
    OverflowError,
    ValueError,
    OSError,
)

_MEDIA_TYPES = mimetypes.MimeTypes()
for _extension, _media_type in (
    (".md", "text/markdown"),
    (".markdown", "text/markdown"),
    (".yml", "application/yaml"),
    (".yaml", "application/yaml"),
    (".toml", "application/toml"),
    (".sql", "application/sql"),
    (".sqlite", "application/vnd.sqlite3"),
    (".sqlite3", "application/vnd.sqlite3"),
    (".db", "application/vnd.sqlite3"),
):
    _MEDIA_TYPES.add_type(_media_type, _extension)


@dataclass(frozen=True)
class PackageLimits:
    max_package_bytes: int = 25 * 1024 * 1024
    max_uncompressed_bytes: int = 100 * 1024 * 1024
    max_file_bytes: int = 50 * 1024 * 1024
    max_files: int = 2000
    max_path_length: int = 255


DEFAULT_LIMITS = PackageLimits()


@dataclass(frozen=True)
class ManifestEntry:
    path: str
    size: int
    sha256: str
    media_type: str
    text: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "size": self.size,
            "sha256": self.sha256,
            "media_type": self.media_type,
            "text": self.text,
        }


@dataclass(frozen=True)
class ParsedPackage:
    kind: Kind
    entrypoint: str
    root_prefix: str
    name: str
    version: str
    display_name: str
    description: str
    category: str | None
    tags: list[str]
    metadata: dict[str, Any]
    manifest: list[ManifestEntry]
    sha256: str
    size_bytes: int
    uncompressed_bytes: int
    file_count: int
    search_text: str


@dataclass(frozen=True)
class _Entry:
    info: zipfile.ZipInfo
    path: str
    is_dir: bool


class _LocalHeader(NamedTuple):
    signature: bytes
    version_needed: int
    flags: int
    method: int
    dos_time: int
    dos_date: int
    crc: int
    compress_size: int
    file_size: int
    name_length: int
    extra_length: int
    offset: int

    @property
    def name_start(self) -> int:
        return self.offset + _LOCAL_HEADER.size

    @property
    def extra_start(self) -> int:
        return self.name_start + self.name_length

    @property
    def data_start(self) -> int:
        return self.extra_start + self.extra_length


@dataclass(frozen=True)
class _Layout:
    kind: Kind
    entrypoint: str
    root_prefix: str


@dataclass(frozen=True)
class _Fields:
    name: str
    description: str
    version: str
    display_name: str
    category: str | None
    tags: list[str]
    metadata: dict[str, Any]


class _CentralDirectoryTooLarge(Exception):
    pass


class _SizeExceeded(Exception):
    pass


class _NotText(Exception):
    pass


class _GuardedStream(io.BytesIO):
    def __init__(self, data: bytes, max_read_bytes: int) -> None:
        super().__init__(data)
        self._max_read_bytes = max_read_bytes

    def read(self, size: int | None = -1) -> bytes:
        if size is not None and size > self._max_read_bytes:
            raise _CentralDirectoryTooLarge
        return super().read(size)


class _GuardedFile:
    __slots__ = ("_handle", "_max_read_bytes")

    def __init__(self, handle: BinaryIO, max_read_bytes: int) -> None:
        self._handle = handle
        self._max_read_bytes = max_read_bytes

    def read(self, size: int | None = -1) -> bytes:
        if size is not None and size > self._max_read_bytes:
            raise _CentralDirectoryTooLarge
        return self._handle.read(size)

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        return self._handle.seek(offset, whence)

    def tell(self) -> int:
        return self._handle.tell()

    def seekable(self) -> bool:
        return True


class _FileData:
    __slots__ = ("_handle", "_lock", "_size")

    def __init__(self, handle: BinaryIO) -> None:
        self._handle = handle
        self._lock = threading.Lock()
        try:
            handle.seek(0, io.SEEK_END)
            self._size = handle.tell()
        except OSError as exc:
            raise StorageError("The stored package cannot be read") from exc

    @property
    def handle(self) -> BinaryIO:
        return self._handle

    def __len__(self) -> int:
        return self._size

    def __getitem__(self, key: slice) -> bytes:
        start, stop, step = key.indices(self._size)
        if step != 1:
            raise TypeError("only contiguous slices of the stored package can be read")
        if stop <= start:
            return b""
        try:
            with self._lock:
                self._handle.seek(start)
                return self._handle.read(stop - start)
        except OSError as exc:
            raise StorageError("The stored package cannot be read") from exc


_Data = bytes | _FileData


class _TextCollector:
    __slots__ = ("_decoder", "_length", "_parts")

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8-sig")()
        self._parts: list[str] = []
        self._length = 0

    def feed(self, chunk: bytes, *, final: bool = False) -> None:
        if b"\x00" in chunk:
            raise _NotText
        try:
            text = self._decoder.decode(chunk, final)
        except UnicodeDecodeError as exc:
            raise _NotText from exc
        room = _ENTRYPOINT_TEXT_CHARS - self._length
        if room > 0 and text:
            part = text[:room]
            self._parts.append(part)
            self._length += len(part)

    def text(self) -> str:
        return "".join(self._parts)


class _MappingScan:
    __slots__ = ("expecting_key", "seen")

    def __init__(self) -> None:
        self.expecting_key = True
        self.seen: set[str] = set()


def _quote(value: object) -> str:
    text = value if isinstance(value, str) else repr(value)
    if len(text) <= _QUOTE_LENGTH:
        return repr(text)
    return repr(text[:_QUOTE_LENGTH]) + "..."


def _invalid(reason: str, message: str) -> InvalidPackageError:
    return InvalidPackageError(reason, message)


def _unpack(layout: struct.Struct, data: _Data, offset: int) -> tuple[Any, ...]:
    if isinstance(data, _FileData):
        return layout.unpack(data[offset : offset + layout.size])
    return layout.unpack_from(data, offset)


def looks_like_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def guess_media_type(path: str) -> str:
    media_type, _ = _MEDIA_TYPES.guess_type(path)
    return media_type or "application/octet-stream"


def is_text_sample(sample: bytes, *, complete: bool = False) -> bool:
    if b"\x00" in sample:
        return False
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        decoder.decode(sample, final=complete)
    except UnicodeDecodeError:
        return False
    return True


def _path_problem(path: str, max_length: int) -> str | None:
    if not path:
        return "path is empty"
    if len(path) > max_length:
        return f"path is longer than {max_length} characters"
    if "\\" in path:
        return "path contains a backslash"
    if _UNSAFE_PATH_CHARACTERS.search(path):
        return "path contains control characters"
    if path.startswith("/"):
        return "path is absolute"
    if _DRIVE_PREFIX.match(path):
        return "path has a drive prefix"
    for segment in path.split("/"):
        if not segment.strip(". "):
            return "path has an empty, '.' or '..' segment"
    return None


def validate_requested_path(path: str, *, max_length: int = DEFAULT_LIMITS.max_path_length) -> str:
    if not isinstance(path, str):
        raise ValidationError("Invalid path: path must be a string", reason="INVALID_PATH")
    problem = _path_problem(path, max_length)
    if problem is not None:
        raise ValidationError(f"Invalid path {_quote(path)}: {problem}", reason="INVALID_PATH")
    return path


def _is_ignored_name(name: str) -> bool:
    segments = name.split("/")
    if len(segments) > 1 and segments[0] == _IGNORED_ROOT_DIRECTORY:
        return True
    basename = name.rstrip("/").rpartition("/")[2]
    return basename in _IGNORED_FILE_NAMES


def _collision_key(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


def _central_directory_cap(limits: PackageLimits) -> int:
    allowed_entries = _ENTRY_ALLOWANCE_FACTOR * limits.max_files + _ENTRY_ALLOWANCE_SLACK
    return max(_MIN_CENTRAL_DIRECTORY_CAP, allowed_entries * _CENTRAL_DIRECTORY_BYTES_PER_ENTRY)


def _looks_like_zip(data: _Data) -> bool:
    return data[:4] in _ZIP_MAGIC_PREFIXES or _END_SIGNATURE in data[-_END_SEARCH_BYTES:]


def _guarded_stream(data: _Data, central_directory_cap: int) -> _GuardedStream | _GuardedFile:
    if isinstance(data, _FileData):
        return _GuardedFile(data.handle, central_directory_cap)
    return _GuardedStream(data, central_directory_cap)


def _open_archive(data: _Data, central_directory_cap: int) -> zipfile.ZipFile:
    if not _looks_like_zip(data):
        raise _invalid("NOT_A_ZIP", "The uploaded file is not a zip archive")
    try:
        return zipfile.ZipFile(_guarded_stream(data, central_directory_cap))
    except _CentralDirectoryTooLarge as exc:
        raise _invalid(
            "TOO_MANY_FILES",
            "The archive central directory is too large for the allowed file count",
        ) from exc
    except _ARCHIVE_ERRORS as exc:
        raise _invalid("CORRUPT_ARCHIVE", f"The archive is corrupt: {exc}") from exc


def _check_envelope(data: bytes, archive: zipfile.ZipFile) -> None:
    offsets = [info.header_offset for info in archive.infolist()]
    if offsets and min(offsets) != 0:
        raise _invalid("CORRUPT_ARCHIVE", "The archive has data before its first entry")
    end = data.rfind(_END_SIGNATURE, max(0, len(data) - _END_SEARCH_BYTES))
    comment_length = int.from_bytes(data[end + 20 : end + 22], "little")
    if end < 0 or end + 22 + comment_length != len(data):
        raise _invalid("CORRUPT_ARCHIVE", "The archive has data after its end record")
    declared_offset = int.from_bytes(data[end + 16 : end + 20], "little")
    if declared_offset not in (_ZIP64_MARKER, archive.start_dir):
        raise _invalid(
            "CORRUPT_ARCHIVE", "The central directory is not where the end record says it is"
        )


def _zip64_end_fields(data: bytes, end: int) -> tuple[int, ...] | None:
    locator_at = end - _ZIP64_LOCATOR.size
    if locator_at < 0 or data[locator_at : locator_at + 4] != _ZIP64_LOCATOR_SIGNATURE:
        return None
    _, locator_disk, record_offset, disks = _ZIP64_LOCATOR.unpack_from(data, locator_at)
    record_at = locator_at - _ZIP64_END_RECORD.size
    if record_at < 0 or record_offset != record_at or locator_disk != 0 or disks > 1:
        raise _invalid("CORRUPT_ARCHIVE", "The ZIP64 end record is not where its locator says")
    record = _ZIP64_END_RECORD.unpack_from(data, record_at)
    if record[0] != _ZIP64_END_SIGNATURE or record[1] != _ZIP64_END_RECORD.size - 12:
        raise _invalid("CORRUPT_ARCHIVE", "The ZIP64 end record is malformed")
    return record[4:]


def _check_central_directory(data: bytes, archive: zipfile.ZipFile) -> None:
    end = data.rfind(_END_SIGNATURE, max(0, len(data) - _END_SEARCH_BYTES))
    declared = _END_RECORD.unpack_from(data, end)[1:7]
    wide = _zip64_end_fields(data, end)
    stop = end
    if wide is not None:
        for short, long, marker in zip(declared, wide, _END_MARKERS, strict=True):
            if short not in (long, marker):
                raise _invalid(
                    "CORRUPT_ARCHIVE", "The ZIP64 end record disagrees with the end record"
                )
        declared = wide
        stop -= _ZIP64_LOCATOR.size + _ZIP64_END_RECORD.size
    position = archive.start_dir
    count = 0
    while position < stop:
        if (
            stop - position < _CENTRAL_HEADER_SIZE
            or data[position : position + 4] != _CENTRAL_SIGNATURE
        ):
            raise _invalid("CORRUPT_ARCHIVE", "The central directory has a malformed record")
        lengths = _CENTRAL_LENGTHS.unpack_from(data, position + _CENTRAL_LENGTHS_OFFSET)
        position += _CENTRAL_HEADER_SIZE + sum(lengths)
        count += 1
    if position != stop:
        raise _invalid(
            "CORRUPT_ARCHIVE",
            "The central directory records do not end where the end record begins",
        )
    disk, directory_disk, entries_here, entries, _, offset = declared
    if (
        disk != 0
        or directory_disk != 0
        or entries_here != count
        or entries != count
        or offset != archive.start_dir
    ):
        raise _invalid(
            "CORRUPT_ARCHIVE", "The end record disagrees with the records of the central directory"
        )


def _check_entry_supported(info: zipfile.ZipInfo) -> None:
    name = _quote(info.orig_filename)
    if stat.S_ISLNK(info.external_attr >> 16):
        raise _invalid("SYMLINK_NOT_ALLOWED", f"Entry {name} is a symbolic link")
    if info.flag_bits & _ENCRYPTION_FLAGS:
        raise _invalid("ENCRYPTED_ENTRY", f"Entry {name} is encrypted")
    if info.compress_type not in _SUPPORTED_COMPRESSION:
        raise _invalid(
            "UNSUPPORTED_COMPRESSION",
            f"Entry {name} uses compression method {info.compress_type}; "
            "only stored and deflate are accepted",
        )


def _checked_entry_path(info: zipfile.ZipInfo, limits: PackageLimits) -> tuple[str, bool]:
    name = info.orig_filename
    if info.filename != name:
        detail = "a NUL byte in its name" if "\x00" in name else "conflicting names"
        raise _invalid("UNSAFE_PATH", f"Entry {_quote(name)} has {detail}")
    is_dir = name.endswith("/")
    path = name[:-1] if is_dir else name
    problem = _path_problem(path, limits.max_path_length)
    if problem is not None:
        raise _invalid("UNSAFE_PATH", f"Unsafe entry path {_quote(name)}: {problem}")
    _check_entry_supported(info)
    if is_dir and info.file_size:
        raise _invalid("CORRUPT_ARCHIVE", f"Directory entry {_quote(name)} has content")
    return path, is_dir


def _collect_entries(archive: zipfile.ZipFile, limits: PackageLimits) -> list[_Entry]:
    entries: list[_Entry] = []
    keys: set[str] = set()
    file_keys: set[str] = set()
    offsets: set[int] = set()
    file_count = 0
    declared_total = 0
    for info in archive.infolist():
        if info.header_offset in offsets:
            raise _invalid(
                "CORRUPT_ARCHIVE",
                f"Entry {_quote(info.orig_filename)} shares its data with another",
            )
        offsets.add(info.header_offset)
        path, is_dir = _checked_entry_path(info, limits)
        name = info.orig_filename
        if _is_ignored_name(name):
            continue
        if not is_dir:
            file_count += 1
            if file_count > limits.max_files:
                raise _invalid(
                    "TOO_MANY_FILES", f"The package has more than {limits.max_files} files"
                )
            if info.file_size > limits.max_file_bytes:
                raise _invalid(
                    "TOO_LARGE",
                    f"File {_quote(name)} exceeds the limit of {limits.max_file_bytes} bytes",
                )
            declared_total += info.file_size
            if declared_total > limits.max_uncompressed_bytes:
                raise _invalid(
                    "TOO_LARGE",
                    f"The package exceeds the uncompressed limit of "
                    f"{limits.max_uncompressed_bytes} bytes",
                )
        key = _collision_key(path)
        if key in keys:
            raise _invalid("DUPLICATE_PATH", f"Duplicate entry path {_quote(name)}")
        keys.add(key)
        if not is_dir:
            file_keys.add(key)
        entries.append(_Entry(info, path, is_dir))
    for entry in entries:
        ancestor = _collision_key(entry.path).rpartition("/")[0]
        while ancestor:
            if ancestor in file_keys:
                raise _invalid(
                    "DUPLICATE_PATH", f"Path {_quote(entry.path)} is used as file and directory"
                )
            ancestor = ancestor.rpartition("/")[0]
    return entries


def _corrupt_local_header(info: zipfile.ZipInfo, problem: str) -> InvalidPackageError:
    return _invalid(
        "CORRUPT_ARCHIVE", f"The local header of {_quote(info.orig_filename)} {problem}"
    )


def _read_local_header(data: _Data, info: zipfile.ZipInfo) -> _LocalHeader:
    offset = info.header_offset
    if offset < 0 or offset + _LOCAL_HEADER.size > len(data):
        raise _corrupt_local_header(info, "lies outside the archive")
    header = _LocalHeader(*_unpack(_LOCAL_HEADER, data, offset), offset)
    if header.signature != _LOCAL_SIGNATURE:
        raise _corrupt_local_header(info, "has a bad signature")
    return header


def _local_sizes(data: _Data, header: _LocalHeader) -> tuple[int, int] | None:
    marked = (header.compress_size == _ZIP64_MARKER, header.file_size == _ZIP64_MARKER)
    if not any(marked):
        return header.compress_size, header.file_size
    if not all(marked):
        return None
    position = header.extra_start
    end = header.data_start
    for _ in range(_MAX_EXTRA_RECORDS):
        tag, length = _unpack(_EXTRA_HEADER, data, position)
        body = position + _EXTRA_HEADER.size
        if body + length > end:
            return None
        if tag == _ZIP64_EXTRA_ID:
            if length < _ZIP64_SIZES.size:
                return None
            file_size, compress_size = _unpack(_ZIP64_SIZES, data, body)
            return compress_size, file_size
        position = body + length
    return None


def _check_data_descriptor(data: bytes, info: zipfile.ZipInfo, start: int, length: int) -> None:
    if start + length > len(data):
        raise _invalid(
            "CORRUPT_ARCHIVE",
            f"The data descriptor of {_quote(info.orig_filename)} lies outside the archive",
        )
    position = start
    if length in _SIGNED_DESCRIPTOR_EXTENTS:
        if data[position : position + len(_DESCRIPTOR_SIGNATURE)] != _DESCRIPTOR_SIGNATURE:
            raise _invalid(
                "CORRUPT_ARCHIVE",
                f"The data descriptor of {_quote(info.orig_filename)} has a bad signature",
            )
        position += len(_DESCRIPTOR_SIGNATURE)
    layout = _WIDE_DESCRIPTOR if length in _WIDE_DESCRIPTOR_EXTENTS else _NARROW_DESCRIPTOR
    if layout.unpack_from(data, position) != (info.CRC, info.compress_size, info.file_size):
        raise _invalid(
            "CORRUPT_ARCHIVE",
            f"The data descriptor of {_quote(info.orig_filename)} disagrees with the "
            "central directory on the sizes or the checksum",
        )


def _check_entry_extent(
    data: bytes, info: zipfile.ZipInfo, header: _LocalHeader, boundary: int
) -> None:
    start = header.data_start + info.compress_size
    gap = boundary - start
    allowed = _DESCRIPTOR_EXTENTS if info.flag_bits & _DESCRIPTOR_FLAG else _EXACT_EXTENTS
    if gap < 0:
        raise _invalid(
            "CORRUPT_ARCHIVE", f"Entry {_quote(info.orig_filename)} overlaps the data that follows"
        )
    if gap not in allowed:
        raise _invalid(
            "CORRUPT_ARCHIVE",
            f"The archive has {gap} bytes after entry {_quote(info.orig_filename)} "
            "that no entry accounts for",
        )
    if gap:
        _check_data_descriptor(data, info, start, gap)


def _check_local_header(data: _Data, info: zipfile.ZipInfo, header: _LocalHeader) -> None:
    encoding = "utf-8" if info.flag_bits & _UTF8_NAME_FLAG else "cp437"
    central_name = info.orig_filename.encode(encoding)
    if (
        header.name_length != len(central_name)
        or data[header.name_start : header.name_start + len(central_name)] != central_name
    ):
        raise _corrupt_local_header(info, "names a different entry")
    if header.method != info.compress_type:
        raise _corrupt_local_header(
            info, "disagrees with the central directory on the compression method"
        )
    if (header.flags ^ info.flag_bits) & _LOCAL_LAYOUT_FLAGS:
        raise _corrupt_local_header(info, "disagrees with the central directory on the flags")
    if info.flag_bits & _DESCRIPTOR_FLAG:
        return
    sizes = _local_sizes(data, header)
    if sizes != (info.compress_size, info.file_size) or header.crc != info.CRC:
        raise _corrupt_local_header(
            info, "disagrees with the central directory on the sizes or the checksum"
        )


def _check_tessellation(data: bytes, archive: zipfile.ZipFile) -> dict[int, _LocalHeader]:
    infos = sorted(archive.infolist(), key=attrgetter("header_offset"))
    starts = [info.header_offset for info in infos]
    starts.append(archive.start_dir)
    headers: dict[int, _LocalHeader] = {}
    for info, boundary in zip(infos, starts[1:], strict=True):
        header = _read_local_header(data, info)
        _check_entry_extent(data, info, header, boundary)
        _check_local_header(data, info, header)
        headers[info.header_offset] = header
    return headers


def _find_wrapper(entries: list[_Entry]) -> str | None:
    tops: set[str] = set()
    for entry in entries:
        head, separator, _ = entry.path.partition("/")
        if not separator and not entry.is_dir:
            return None
        tops.add(head)
    if len(tops) != 1:
        return None
    return next(iter(tops)) + "/"


def _entrypoint_hits(file_paths: set[str], prefix: str) -> list[str]:
    return [name for name in ENTRYPOINT_KINDS if prefix + name in file_paths]


def _resolve_layout(entries: list[_Entry]) -> _Layout:
    file_paths = {entry.path for entry in entries if not entry.is_dir}
    prefix = ""
    hits = _entrypoint_hits(file_paths, prefix)
    if not hits:
        wrapper = _find_wrapper(entries)
        if wrapper is not None:
            prefix = wrapper
            hits = _entrypoint_hits(file_paths, prefix)
    if len(hits) > 1:
        raise _invalid(
            "AMBIGUOUS_ENTRYPOINT",
            f"The package contains both {SKILL_ENTRYPOINT} and {GROUNDING_ENTRYPOINT}",
        )
    if not hits:
        raise _invalid(
            "MISSING_ENTRYPOINT",
            f"The package must contain {SKILL_ENTRYPOINT} or {GROUNDING_ENTRYPOINT} at its root",
        )
    return _Layout(ENTRYPOINT_KINDS[hits[0]], hits[0], prefix)


def _stored_chunks(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> Iterator[bytes]:
    probe = copy.copy(info)
    probe.file_size = _UNBOUNDED_SIZE
    with archive.open(probe) as stream:
        while True:
            chunk = stream.read(_CHUNK_BYTES)
            if not chunk:
                return
            yield chunk


def _deflated_chunks(
    data: _Data, info: zipfile.ZipInfo, header: _LocalHeader, *, check_crc: bool = True
) -> Iterator[bytes]:
    if info.flag_bits & _PATCHED_DATA_FLAG:
        raise NotImplementedError("compressed patched data (flag bit 5)")
    position = header.data_start
    end = position + info.compress_size
    if end > len(data):
        raise zipfile.BadZipFile("the data of the entry lies beyond the end of the archive")
    crc = 0
    if info.compress_size:
        decompressor = zlib.decompressobj(-zlib.MAX_WBITS)
        fed = position
        for start in range(position, end, _CHUNK_BYTES):
            fed = min(start + _CHUNK_BYTES, end)
            pending = data[start:fed]
            while pending and not decompressor.eof:
                chunk = decompressor.decompress(pending, _CHUNK_BYTES)
                pending = decompressor.unconsumed_tail
                if chunk:
                    crc = zlib.crc32(chunk, crc)
                    yield chunk
            if decompressor.eof:
                break
        while not decompressor.eof:
            chunk = decompressor.decompress(b"", _CHUNK_BYTES)
            if not chunk:
                break
            crc = zlib.crc32(chunk, crc)
            yield chunk
        if not decompressor.eof:
            raise zipfile.BadZipFile("the deflate stream is not terminated")
        slack = len(decompressor.unused_data) + end - fed
        if slack:
            raise zipfile.BadZipFile(f"{slack} bytes follow the end of the deflate stream")
    if check_crc and crc != info.CRC:
        raise zipfile.BadZipFile(f"Bad CRC-32 for file {info.orig_filename!r}")


def _iter_chunks(
    data: _Data,
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    header: _LocalHeader,
    *,
    limit: int,
) -> Iterator[bytes]:
    if info.compress_type == zipfile.ZIP_DEFLATED:
        source = _deflated_chunks(data, info, header)
    else:
        source = _stored_chunks(archive, info)
    total = 0
    try:
        for chunk in source:
            total += len(chunk)
            if total > limit:
                allowed = len(chunk) - (total - limit)
                if allowed > 0:
                    yield chunk[:allowed]
                raise _SizeExceeded
            yield chunk
    except _READ_ERRORS as exc:
        raise _invalid(
            "CORRUPT_ARCHIVE", f"Entry {_quote(info.orig_filename)} cannot be read: {exc}"
        ) from exc
    if total != info.file_size:
        raise _invalid(
            "CORRUPT_ARCHIVE",
            f"Entry {_quote(info.orig_filename)} declares {info.file_size} bytes "
            f"but contains {total}",
        )


def _drain(chunks: Iterable[bytes], limit: int) -> int:
    total = 0
    for chunk in chunks:
        total += len(chunk)
        if total > limit:
            raise _SizeExceeded
    return total


def _verify_unread_streams(
    data: bytes, archive: zipfile.ZipFile, headers: dict[int, _LocalHeader]
) -> None:
    budget = _UNREAD_STREAM_BYTES
    for info in archive.infolist():
        name = info.orig_filename
        is_directory = name.endswith("/")
        if info.compress_type != zipfile.ZIP_DEFLATED or not (
            is_directory or _is_ignored_name(name)
        ):
            continue
        stream = _deflated_chunks(data, info, headers[info.header_offset], check_crc=False)
        try:
            budget -= _drain(stream, 0 if is_directory else budget)
        except _SizeExceeded:
            if is_directory:
                raise _invalid(
                    "CORRUPT_ARCHIVE", f"Directory entry {_quote(name)} has content"
                ) from None
            raise _invalid(
                "TOO_LARGE",
                f"The ignored entries inflate to more than {_UNREAD_STREAM_BYTES} bytes",
            ) from None
        except _READ_ERRORS as exc:
            raise _invalid(
                "CORRUPT_ARCHIVE", f"Entry {_quote(name)} cannot be read: {exc}"
            ) from exc


def _read_files(
    data: bytes,
    archive: zipfile.ZipFile,
    headers: dict[int, _LocalHeader],
    entries: list[_Entry],
    layout: _Layout,
    limits: PackageLimits,
) -> tuple[list[ManifestEntry], str]:
    manifest: list[ManifestEntry] = []
    entrypoint_text = ""
    consumed = 0
    for entry in entries:
        if entry.is_dir:
            continue
        relative = entry.path[len(layout.root_prefix) :]
        remaining = limits.max_uncompressed_bytes - consumed
        digest = hashlib.sha256()
        sample = bytearray()
        collector = _TextCollector() if relative == layout.entrypoint else None
        size = 0
        try:
            for chunk in _iter_chunks(
                data,
                archive,
                entry.info,
                headers[entry.info.header_offset],
                limit=min(limits.max_file_bytes, remaining),
            ):
                size += len(chunk)
                digest.update(chunk)
                if len(sample) < TEXT_SAMPLE_BYTES:
                    sample += chunk[: TEXT_SAMPLE_BYTES - len(sample)]
                if collector is not None:
                    collector.feed(chunk)
            if collector is not None:
                collector.feed(b"", final=True)
                entrypoint_text = collector.text()
        except _SizeExceeded:
            if limits.max_file_bytes <= remaining:
                message = (
                    f"File {_quote(entry.path)} exceeds the limit of {limits.max_file_bytes} bytes"
                )
            else:
                message = (
                    f"The package exceeds the uncompressed limit of "
                    f"{limits.max_uncompressed_bytes} bytes"
                )
            raise _invalid("TOO_LARGE", message) from None
        except _NotText:
            raise _invalid(
                "BAD_FRONTMATTER", f"{layout.entrypoint} is not valid UTF-8 text without NUL bytes"
            ) from None
        consumed += size
        manifest.append(
            ManifestEntry(
                path=relative,
                size=size,
                sha256=digest.hexdigest(),
                media_type=guess_media_type(relative),
                text=is_text_sample(bytes(sample), complete=size <= TEXT_SAMPLE_BYTES),
            )
        )
    manifest.sort(key=lambda item: item.path)
    return manifest, entrypoint_text


def split_frontmatter(text: str, entrypoint: str = _ENTRYPOINT_LABEL) -> tuple[str, str]:
    opening = _OPENING_DELIMITER.match(text)
    if opening is None:
        raise _invalid(
            "BAD_FRONTMATTER", f"{entrypoint} must start with a YAML block delimited by '---' lines"
        )
    closing = _CLOSING_DELIMITER.search(text, opening.end())
    if closing is None:
        raise _invalid(
            "BAD_FRONTMATTER", f"The frontmatter of {entrypoint} is not closed by a '---' line"
        )
    block = text[opening.end() : closing.start()]
    if len(block.encode("utf-8", "surrogatepass")) > FRONTMATTER_MAX_BYTES:
        raise _invalid(
            "BAD_FRONTMATTER", f"The frontmatter exceeds the limit of {FRONTMATTER_MAX_BYTES} bytes"
        )
    return block, text[closing.end() :]


def _check_flow_depth(block: str) -> None:
    depth = 0
    for character in block:
        if character in "[{":
            depth += 1
            if depth > _MAX_FLOW_DEPTH:
                raise _invalid("BAD_FRONTMATTER", "The frontmatter is nested too deeply")
        elif character in "]}" and depth:
            depth -= 1


def _scan_yaml_events(block: str) -> None:
    stack: list[_MappingScan | None] = []
    for event in yaml.parse(block, Loader=yaml.SafeLoader):
        if isinstance(event, yaml.NodeEvent) and event.anchor is not None:
            raise _invalid("BAD_FRONTMATTER", "YAML anchors and aliases are not allowed")
        if isinstance(event, yaml.MappingEndEvent | yaml.SequenceEndEvent):
            stack.pop()
            continue
        if not isinstance(
            event, yaml.ScalarEvent | yaml.MappingStartEvent | yaml.SequenceStartEvent
        ):
            continue
        parent = stack[-1] if stack else None
        if parent is not None:
            if parent.expecting_key and isinstance(event, yaml.ScalarEvent):
                if event.value in parent.seen:
                    raise _invalid(
                        "BAD_FRONTMATTER", f"Duplicate key {_quote(event.value)} in the frontmatter"
                    )
                parent.seen.add(event.value)
            parent.expecting_key = not parent.expecting_key
        if isinstance(event, yaml.MappingStartEvent):
            stack.append(_MappingScan())
        elif isinstance(event, yaml.SequenceStartEvent):
            stack.append(None)


def _describe_yaml_error(exc: BaseException) -> str:
    if isinstance(exc, yaml.MarkedYAMLError) and exc.problem:
        mark = exc.problem_mark or exc.context_mark
        if mark is not None:
            return f"{exc.problem} (line {mark.line + 2}, column {mark.column + 1})"
        return str(exc.problem)
    lines = str(exc).strip().splitlines()
    return lines[0][:200] if lines else type(exc).__name__


def _load_mapping(block: str) -> dict[str, Any]:
    try:
        _check_flow_depth(block)
        _scan_yaml_events(block)
        loaded = yaml.safe_load(block)
    except InvalidPackageError:
        raise
    except (yaml.YAMLError, RecursionError, ValueError, OverflowError) as exc:
        raise _invalid(
            "BAD_FRONTMATTER", f"The frontmatter is not valid YAML: {_describe_yaml_error(exc)}"
        ) from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise _invalid("BAD_FRONTMATTER", "The frontmatter must be a YAML mapping")
    for key in loaded:
        if not isinstance(key, str):
            raise _invalid("BAD_FRONTMATTER", "All frontmatter keys must be strings")
    return loaded


def _checked_text(field: str, value: str) -> str:
    if _CONTROL_TEXT.search(value):
        raise _invalid("INVALID_FIELD", f"'{field}' contains control characters")
    return value


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, str):
        return _checked_text("metadata", value)
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, dict):
        return {_jsonable_key(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    return _checked_text("metadata", str(value))


def _jsonable_key(key: Any) -> str:
    if isinstance(key, str):
        return _checked_text("metadata", key)
    if isinstance(key, datetime | date):
        return key.isoformat()
    return _checked_text("metadata", str(key))


def _read_name(value: Any) -> str:
    if value is None:
        raise _invalid("MISSING_FIELD", "The frontmatter field 'name' is required")
    if not isinstance(value, str):
        raise _invalid("INVALID_FIELD", "The frontmatter field 'name' must be a string")
    if not 1 <= len(value) <= NAME_MAX_LENGTH or _NAME_PATTERN.fullmatch(value) is None:
        raise _invalid(
            "INVALID_FIELD",
            f"The name {_quote(value)} must have 1 to {NAME_MAX_LENGTH} characters: lowercase "
            "letters and digits separated by single hyphens",
        )
    if looks_like_uuid(value):
        raise _invalid("INVALID_FIELD", f"The name {_quote(value)} must not look like a UUID")
    return value


def _read_description(value: Any) -> str:
    if value is None:
        raise _invalid("MISSING_FIELD", "The frontmatter field 'description' is required")
    if not isinstance(value, str):
        raise _invalid("INVALID_FIELD", "The frontmatter field 'description' must be a string")
    description = value.strip()
    if not 1 <= len(description) <= DESCRIPTION_MAX_LENGTH:
        raise _invalid(
            "INVALID_FIELD",
            f"The description must have 1 to {DESCRIPTION_MAX_LENGTH} characters",
        )
    return _checked_text("description", description)


def _read_version(value: Any, requested: str | None) -> str:
    declared: str | None = None
    if value is not None:
        if not isinstance(value, str):
            hint = ""
            if isinstance(value, int | float) and not isinstance(value, bool):
                hint = (
                    f' YAML read {value!r} as a number: quote it, for example version: "{value}".'
                )
            raise _invalid(
                "INVALID_FIELD", f"The frontmatter field 'version' must be a string.{hint}"
            )
        try:
            semver.parse(value)
        except semver.InvalidVersionError as exc:
            raise _invalid("INVALID_FIELD", f"The frontmatter version is invalid: {exc}") from exc
        declared = value
    if requested is not None:
        if declared is not None and declared != requested:
            raise _invalid(
                "VERSION_MISMATCH",
                f"The frontmatter version {declared!r} differs from the requested {requested!r}",
            )
        return requested
    if declared is None:
        raise _invalid(
            "MISSING_FIELD",
            "The version is required: set it in the frontmatter or provide it when uploading",
        )
    return declared


def _default_display_name(name: str) -> str:
    return " ".join(part.capitalize() for part in name.split("-"))


def _read_display_name(value: Any, name: str) -> str:
    if value is None:
        return _default_display_name(name)
    if not isinstance(value, str):
        raise _invalid("INVALID_FIELD", "The frontmatter field 'display_name' must be a string")
    display_name = value.strip()
    if not display_name:
        return _default_display_name(name)
    if len(display_name) > DISPLAY_NAME_MAX_LENGTH:
        raise _invalid(
            "INVALID_FIELD",
            f"The display_name must have at most {DISPLAY_NAME_MAX_LENGTH} characters",
        )
    return _checked_text("display_name", display_name)


def _read_tags(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise _invalid("INVALID_FIELD", "The frontmatter field 'tags' must be a list of strings")
    tags: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise _invalid("INVALID_FIELD", f"The tag {_quote(item)} must be a string")
        tag = item.strip().lower()
        if _TAG_PATTERN.fullmatch(tag) is None:
            raise _invalid(
                "INVALID_FIELD",
                f"The tag {_quote(item)} is invalid: use 1 to 32 characters among lowercase "
                "letters, digits, '.', '_', '+' and '-', starting with a letter or digit",
            )
        if tag not in tags:
            tags.append(tag)
    if len(tags) > TAGS_MAX_COUNT:
        raise _invalid("INVALID_FIELD", f"The package can have at most {TAGS_MAX_COUNT} tags")
    return tags


def _read_category(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _invalid("INVALID_FIELD", "The frontmatter field 'category' must be a string")
    category = value.strip().lower()
    if not category:
        return None
    if _CATEGORY_PATTERN.fullmatch(category) is None:
        raise _invalid(
            "INVALID_FIELD",
            f"The category {_quote(value)} is invalid: use 1 to 64 characters among lowercase "
            "letters, digits and '-', starting with a letter or digit",
        )
    return category


def _check_kind(value: Any, entrypoint_kind: Kind) -> None:
    if value is None:
        return
    if value not in ("skill", "grounding"):
        raise _invalid(
            "INVALID_FIELD", "The frontmatter field 'kind' must be 'skill' or 'grounding'"
        )
    if value != entrypoint_kind:
        raise _invalid(
            "KIND_MISMATCH",
            f"The frontmatter kind {value!r} does not match the {entrypoint_kind} entry point",
        )


def _build_metadata(remaining: dict[str, Any]) -> dict[str, Any]:
    try:
        metadata = _jsonable(remaining)
        json.dumps(metadata, allow_nan=False, ensure_ascii=False).encode("utf-8")
    except InvalidPackageError as exc:
        raise _invalid("BAD_FRONTMATTER", f"Invalid frontmatter value: {exc}") from exc
    except (RecursionError, TypeError, ValueError) as exc:
        raise _invalid("BAD_FRONTMATTER", "The frontmatter metadata cannot be serialized") from exc
    return metadata


def _extract_fields(
    mapping: dict[str, Any], entrypoint_kind: Kind, requested_version: str | None
) -> _Fields:
    remaining = dict(mapping)
    name = _read_name(remaining.pop("name", None))
    description = _read_description(remaining.pop("description", None))
    version = _read_version(remaining.pop("version", None), requested_version)
    display_name = _read_display_name(remaining.pop("display_name", None), name)
    tags = _read_tags(remaining.pop("tags", None))
    category = _read_category(remaining.pop("category", None))
    _check_kind(remaining.pop("kind", None), entrypoint_kind)
    return _Fields(
        name=name,
        description=description,
        version=version,
        display_name=display_name,
        category=category,
        tags=tags,
        metadata=_build_metadata(remaining),
    )


def _make_search_text(body: str) -> str:
    return _CONTROL_TEXT.sub("", body.strip()[:SEARCH_TEXT_MAX_CHARS])


def inspect_package(
    data: bytes, limits: PackageLimits = DEFAULT_LIMITS, *, version: str | None = None
) -> ParsedPackage:
    if version is not None:
        semver.parse(version)
    if len(data) > limits.max_package_bytes:
        raise _invalid(
            "TOO_LARGE", f"The package exceeds the limit of {limits.max_package_bytes} bytes"
        )
    with _open_archive(data, _central_directory_cap(limits)) as archive:
        _check_envelope(data, archive)
        _check_central_directory(data, archive)
        entries = _collect_entries(archive, limits)
        headers = _check_tessellation(data, archive)
        layout = _resolve_layout(entries)
        _verify_unread_streams(data, archive, headers)
        manifest, text = _read_files(data, archive, headers, entries, layout, limits)
    block, body = split_frontmatter(text, layout.entrypoint)
    fields = _extract_fields(_load_mapping(block), layout.kind, version)
    return ParsedPackage(
        kind=layout.kind,
        entrypoint=layout.entrypoint,
        root_prefix=layout.root_prefix,
        name=fields.name,
        version=fields.version,
        display_name=fields.display_name,
        description=fields.description,
        category=fields.category,
        tags=fields.tags,
        metadata=fields.metadata,
        manifest=manifest,
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        uncompressed_bytes=sum(item.size for item in manifest),
        file_count=len(manifest),
        search_text=_make_search_text(body),
    )


def _checked_root_prefix(root_prefix: str) -> str:
    if root_prefix == "":
        return root_prefix
    directory = root_prefix[:-1]
    if not root_prefix.endswith("/") or "/" in directory or _path_problem(directory, 255):
        raise StorageError(f"Invalid root prefix: {root_prefix!r}")
    return root_prefix


def _check_max_bytes(max_bytes: int | None) -> None:
    if max_bytes is not None and max_bytes < 0:
        raise ValidationError("max_bytes must not be negative")


def _checked_target(root_prefix: str, path: str) -> str:
    validate_requested_path(path)
    return _checked_root_prefix(root_prefix) + path


def _find_member(archive: zipfile.ZipFile, target: str, path: str) -> zipfile.ZipInfo:
    matches = [info for info in archive.infolist() if info.orig_filename == target]
    if not matches or _is_ignored_name(target):
        raise NotFoundError(f"File not found: {path}")
    if len(matches) > 1:
        raise _invalid("DUPLICATE_PATH", f"Duplicate entry path {_quote(target)}")
    info = matches[0]
    if info.filename != info.orig_filename:
        raise _invalid("UNSAFE_PATH", f"Entry {_quote(target)} has conflicting names")
    _check_entry_supported(info)
    return info


def _more_than_declared(info: zipfile.ZipInfo) -> InvalidPackageError:
    return _invalid(
        "CORRUPT_ARCHIVE",
        f"Entry {_quote(info.orig_filename)} contains more data than the {info.file_size} "
        "bytes it declares",
    )


class PackageMember:
    __slots__ = ("_archive", "_data", "_header", "_info")

    def __init__(
        self,
        data: _Data,
        archive: zipfile.ZipFile,
        info: zipfile.ZipInfo,
        header: _LocalHeader,
    ) -> None:
        self._data = data
        self._archive = archive
        self._info = info
        self._header = header

    @property
    def size(self) -> int:
        return self._info.file_size

    def read(self, max_bytes: int | None = None, *, truncate: bool = False) -> bytes:
        _check_max_bytes(max_bytes)
        return _read_entry(self._data, self._archive, self._info, self._header, max_bytes, truncate)

    def chunks(self) -> Iterator[bytes]:
        try:
            yield from _iter_chunks(
                self._data, self._archive, self._info, self._header, limit=self._info.file_size
            )
        except _SizeExceeded:
            raise _more_than_declared(self._info) from None

    def close(self) -> None:
        self._archive.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _open_member(data: _Data, target: str, path: str) -> PackageMember:
    archive = _open_archive(data, _READ_CENTRAL_DIRECTORY_CAP)
    try:
        info = _find_member(archive, target, path)
        header = _read_local_header(data, info)
        _check_local_header(data, info, header)
    except BaseException:
        archive.close()
        raise
    return PackageMember(data, archive, info, header)


def open_package_member(stream: BinaryIO, root_prefix: str, path: str) -> PackageMember:
    target = _checked_target(root_prefix, path)
    return _open_member(_FileData(stream), target, path)


def read_package_file(
    data: bytes,
    root_prefix: str,
    path: str,
    *,
    max_bytes: int | None = None,
    truncate: bool = False,
) -> bytes:
    target = _checked_target(root_prefix, path)
    _check_max_bytes(max_bytes)
    with _open_member(data, target, path) as member:
        return member.read(max_bytes, truncate=truncate)


def _read_entry(
    data: _Data,
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    header: _LocalHeader,
    max_bytes: int | None,
    truncate: bool,
) -> bytes:
    declared = info.file_size
    capped = max_bytes is not None and max_bytes < declared
    limit = max_bytes if capped and max_bytes is not None else declared
    buffer = bytearray()
    try:
        for chunk in _iter_chunks(data, archive, info, header, limit=limit):
            buffer += chunk
    except _SizeExceeded:
        if capped and truncate:
            return bytes(buffer)
        if capped:
            raise _invalid(
                "TOO_LARGE",
                f"File {_quote(info.orig_filename)} exceeds the requested limit of {max_bytes} bytes",
            ) from None
        raise _more_than_declared(info) from None
    return bytes(buffer)
