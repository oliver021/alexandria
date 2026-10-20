from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import ipaddress
import json
import os
import re
import secrets
import stat
import sys
import threading
import unicodedata
import zipfile
import zlib
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, redirect_stderr, redirect_stdout, suppress
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any, BinaryIO, Self, TextIO
from urllib.parse import quote, urlsplit

import httpx

from app.services import semver
from app.services.errors import InvalidPackageError, ValidationError
from app.services.package_inspector import (
    DEFAULT_LIMITS,
    ManifestEntry,
    PackageLimits,
    ParsedPackage,
    inspect_package,
    looks_like_uuid,
    validate_requested_path,
)

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130

DEFAULT_URL = "http://localhost:8000"
URL_ENV = "ALX_URL"
TOKEN_ENV = "ALX_TOKEN"
DEBUG_ENV = "ALX_DEBUG"
API_PREFIX = "/api/v1"
LATEST = "latest"
FALLBACK_VERSION = "0.1.0"

DEFAULT_SEARCH_LIMIT = 20
MAX_SEARCH_LIMIT = 100
LISTED_FILES = 50
CONFLICTS_SHOWN = 5

ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
ZIP_COMPRESSION_LEVEL = 9
UNIX_SYSTEM = 3
FILE_MODE = 0o644
EXECUTABLE_MODE = 0o755
DIRECTORY_MODE = 0o755
EXCLUDED_NAMES = frozenset({".git", "__pycache__", ".DS_Store", "node_modules", "Thumbs.db"})
EXCLUDED_SUFFIX = ".pyc"
ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06")

CHUNK_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
REQUEST_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
DESCRIPTION_COLUMN_WIDTH = 60

_NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_MAX_NAME_LENGTH = 64
_SHA256_PATTERN = re.compile(r"[0-9a-fA-F]{64}")
_BEARER_CREDENTIAL = re.compile(r"[A-Za-z0-9._~+/-]+=*")
_DISPOSITION_FILENAME = re.compile(r'filename="([^"\\\r\n]{1,255})"')
_SAFE_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,200}\.zip")
_ARTIFACT_NAME = re.compile(
    r"(?P<name>[a-z0-9]+(?:-[a-z0-9]+)*)-(?P<version>\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)"
)

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_BINARY = getattr(os, "O_BINARY", 0)
_CREATE_NEW = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _BINARY
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_READ_ONLY = os.O_RDONLY | _NOFOLLOW | _BINARY | _NONBLOCK
_IS_WINDOWS = os.name == "nt"
_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{number}" for number in range(1, 10)}
    | {f"lpt{number}" for number in range(1, 10)}
)

_READ_ERRORS = (
    zipfile.BadZipFile,
    zlib.error,
    EOFError,
    RuntimeError,
    NotImplementedError,
    OverflowError,
    ValueError,
)
_NETWORK_ERRORS = (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError)


class CliError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: str = "ERROR",
        reason: str | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.reason = reason
        self.status = status


class UsageError(CliError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="USAGE")


class _ParserExit(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


Transport = httpx.BaseTransport | httpx.AsyncBaseTransport


@dataclass(frozen=True)
class Context:
    env: Mapping[str, str] = field(repr=False)
    transport: Transport | None = None


@dataclass(frozen=True)
class SourceFile:
    path: str
    location: str
    executable: bool
    size: int


@dataclass(frozen=True)
class LoadedPackage:
    data: bytes
    parsed: ParsedPackage
    filename: str


@dataclass(frozen=True)
class Download:
    sha256: str
    size: int
    filename: str | None


@dataclass(frozen=True)
class Remote:
    url: str
    token: str | None = field(repr=False)


def _clean(value: object) -> str:
    text = " ".join(str(value).split())
    return "".join(character if character.isprintable() else "?" for character in text)


def _shown(path: object) -> str:
    return f"'{_clean(path)}'"


def _human_bytes(count: int) -> str:
    if count < 1024:
        return f"{count} B"
    value = float(count)
    for unit in ("KiB", "MiB"):
        value /= 1024
        if round(value, 1) < 1024:
            return f"{value:.1f} {unit}"
    return f"{value / 1024:.1f} GiB"


def _os_reason(exc: OSError) -> str:
    return _clean(exc.strerror or exc)


def _emit_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=True))


def _print_fields(rows: Sequence[tuple[str, str]]) -> None:
    width = max(len(label) for label, _ in rows)
    for label, value in rows:
        print(f"  {label.ljust(width)}  {_clean(value)}")


def _error_payload(exc: CliError) -> dict[str, str]:
    error = {"code": exc.code, "message": exc.message}
    if exc.reason:
        error["reason"] = exc.reason
    return error


def _print_error(exc: CliError, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"error": _error_payload(exc)}, ensure_ascii=True), file=sys.stderr)
        return
    label = ""
    if exc.code not in ("ERROR", "USAGE"):
        label = f"{exc.code} ({exc.reason}): " if exc.reason else f"{exc.code}: "
    print(f"error: {label}{_clean(exc.message)}", file=sys.stderr)


def _invalid_package(exc: InvalidPackageError) -> CliError:
    return CliError(exc.message, code=exc.code, reason=exc.reason)


def _package_version() -> str:
    with suppress(metadata.PackageNotFoundError):
        return metadata.version("alexandria")
    return FALLBACK_VERSION


def _is_excluded(name: str) -> bool:
    return name in EXCLUDED_NAMES or name.endswith(EXCLUDED_SUFFIX)


def _describe_entry(entry: os.DirEntry[str]) -> tuple[str, str, str, int, int]:
    try:
        info = entry.stat(follow_symlinks=False)
    except OSError as exc:
        raise CliError(f"cannot inspect {_shown(entry.path)}: {_os_reason(exc)}") from exc
    mode = info.st_mode
    if stat.S_ISLNK(mode):
        kind = "link"
    elif stat.S_ISDIR(mode):
        kind = "directory"
    elif stat.S_ISREG(mode):
        kind = "file"
    else:
        kind = "special"
    return entry.name, entry.path, kind, mode, info.st_size


def _scan(directory: str) -> list[tuple[str, str, str, int, int]]:
    try:
        with os.scandir(directory) as iterator:
            rows = [_describe_entry(entry) for entry in iterator if not _is_excluded(entry.name)]
    except OSError as exc:
        raise CliError(f"cannot read directory {_shown(directory)}: {_os_reason(exc)}") from exc
    rows.sort(key=lambda row: row[0])
    return rows


def collect_files(
    root: Path,
    limits: PackageLimits = DEFAULT_LIMITS,
    *,
    exclude: frozenset[str] = frozenset(),
) -> list[SourceFile]:
    base = os.path.realpath(root)
    skipped_names = {os.path.basename(item) for item in exclude}
    files: list[SourceFile] = []
    total = 0
    pending = [(base, "")]
    while pending:
        directory, prefix = pending.pop()
        for name, location, kind, mode, size in _scan(directory):
            relative = prefix + name
            if name in skipped_names and os.path.realpath(location) in exclude:
                continue
            try:
                relative.encode("utf-8")
            except UnicodeEncodeError:
                raise InvalidPackageError(
                    "UNSAFE_PATH", f"Path {relative!r} cannot be encoded as UTF-8"
                ) from None
            if kind == "link":
                raise InvalidPackageError(
                    "SYMLINK_NOT_ALLOWED",
                    f"{_shown(relative)} is a symbolic link; packages cannot contain symbolic links",
                )
            if kind == "special":
                raise InvalidPackageError(
                    "UNSAFE_PATH", f"{_shown(relative)} is neither a regular file nor a directory"
                )
            if kind == "directory":
                pending.append((location, relative + "/"))
                continue
            if len(files) >= limits.max_files:
                raise InvalidPackageError(
                    "TOO_MANY_FILES", f"The package has more than {limits.max_files} files"
                )
            if size > limits.max_file_bytes:
                raise InvalidPackageError(
                    "TOO_LARGE",
                    f"File {_shown(relative)} exceeds the limit of {limits.max_file_bytes} bytes",
                )
            total += size
            if total > limits.max_uncompressed_bytes:
                raise InvalidPackageError(
                    "TOO_LARGE",
                    f"The package exceeds the uncompressed limit of "
                    f"{limits.max_uncompressed_bytes} bytes",
                )
            files.append(SourceFile(relative, location, bool(mode & stat.S_IXUSR), size))
    files.sort(key=lambda item: item.path)
    return files


def _read_source(item: SourceFile, limits: PackageLimits) -> bytes:
    try:
        with os.fdopen(os.open(item.location, _READ_ONLY), "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise InvalidPackageError(
                    "UNSAFE_PATH", f"{_shown(item.path)} is neither a regular file nor a directory"
                )
            content = handle.read(limits.max_file_bytes + 1)
    except OSError as exc:
        raise CliError(f"cannot read {_shown(item.path)}: {_os_reason(exc)}") from exc
    if len(content) > limits.max_file_bytes:
        raise InvalidPackageError(
            "TOO_LARGE",
            f"File {_shown(item.path)} exceeds the limit of {limits.max_file_bytes} bytes",
        )
    return content


def _build_zip(files: Sequence[SourceFile], limits: PackageLimits) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(
        buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=ZIP_COMPRESSION_LEVEL
    ) as archive:
        for item in files:
            info = zipfile.ZipInfo(item.path, date_time=ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = UNIX_SYSTEM
            mode = EXECUTABLE_MODE if item.executable else FILE_MODE
            info.external_attr = (stat.S_IFREG | mode) << 16
            archive.writestr(info, _read_source(item, limits), compresslevel=ZIP_COMPRESSION_LEVEL)
    return buffer.getvalue()


def _assemble(
    files: Sequence[SourceFile], limits: PackageLimits, version: str | None
) -> tuple[bytes, ParsedPackage]:
    data = _build_zip(files, limits)
    return data, inspect_package(data, limits, version=version)


def pack_directory(
    root: Path,
    *,
    version: str | None = None,
    limits: PackageLimits = DEFAULT_LIMITS,
    exclude: frozenset[str] = frozenset(),
) -> LoadedPackage:
    files = collect_files(root, limits, exclude=exclude)
    data, parsed = _assemble(files, limits, version)
    filename = f"{parsed.name}-{parsed.version}.zip"
    if any(item.path == filename for item in files):
        files = [item for item in files if item.path != filename]
        data, parsed = _assemble(files, limits, version)
    return LoadedPackage(data=data, parsed=parsed, filename=filename)


def load_package(
    path: Path,
    *,
    version: str | None = None,
    limits: PackageLimits = DEFAULT_LIMITS,
    exclude: frozenset[str] = frozenset(),
) -> LoadedPackage:
    if path.is_dir():
        return pack_directory(path, version=version, limits=limits, exclude=exclude)
    if not path.is_file():
        raise UsageError(f"{_shown(path)} is neither a file nor a directory")
    try:
        with path.open("rb") as handle:
            data = handle.read(limits.max_package_bytes + 1)
    except OSError as exc:
        raise CliError(f"cannot read {_shown(path)}: {_os_reason(exc)}") from exc
    parsed = inspect_package(data, limits, version=version)
    return LoadedPackage(data=data, parsed=parsed, filename=f"{parsed.name}-{parsed.version}.zip")


def _missing_chain(directory: Path) -> list[Path]:
    missing: list[Path] = []
    current = Path(os.path.abspath(directory))
    while not current.exists():
        missing.append(current)
        current = current.parent
    return missing[::-1]


class _Part:
    def __init__(self, directory: Path) -> None:
        self._created: list[Path] = []
        self._committed = False
        self.path = directory / f".{secrets.token_hex(8)}.alx-part"
        try:
            for missing in _missing_chain(directory):
                os.mkdir(missing, DIRECTORY_MODE)
                self._created.append(missing)
            self.handle: BinaryIO = os.fdopen(os.open(self.path, _CREATE_NEW, 0o666), "wb")
        except BaseException:
            self._remove_created()
            raise

    def _remove_created(self) -> None:
        for missing in reversed(self._created):
            with suppress(OSError):
                os.rmdir(missing)

    def commit(self, final: Path) -> None:
        self.handle.close()
        os.replace(self.path, final)
        self._committed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.handle.close()
        if not self._committed:
            with suppress(FileNotFoundError):
                os.unlink(self.path)
            self._remove_created()


def _write_file(final: Path, data: bytes) -> None:
    with _Part(final.parent) as part:
        part.handle.write(data)
        part.commit(final)


def _windows_problem(segment: str) -> str | None:
    stem = segment.split(".", 1)[0].rstrip(" ").lower()
    if stem in _WINDOWS_RESERVED:
        return "is a reserved device name on Windows"
    if segment.endswith((".", " ")):
        return "ends with a dot or a space"
    if ":" in segment:
        return "contains a colon"
    return None


def _checked_parts(path: str) -> tuple[str, ...]:
    try:
        validate_requested_path(path)
    except ValidationError as exc:
        raise InvalidPackageError("UNSAFE_PATH", exc.message) from exc
    parts = tuple(path.split("/"))
    if _IS_WINDOWS:
        for part in parts:
            problem = _windows_problem(part)
            if problem is not None:
                raise InvalidPackageError("UNSAFE_PATH", f"Segment {_shown(part)} {problem}")
    return parts


def _assert_inside(root: Path, target: Path) -> None:
    root_text, target_text = os.fspath(root), os.fspath(target)
    if os.path.commonpath([root_text, target_text]) != root_text or target_text == root_text:
        raise InvalidPackageError(
            "UNSAFE_PATH", f"{_shown(target_text)} resolves outside of the destination directory"
        )


@dataclass(frozen=True)
class _Planned:
    entry: ManifestEntry
    info: zipfile.ZipInfo
    parts: tuple[str, ...]
    mode: int


@dataclass
class _Workspace:
    directories: list[Path] = field(default_factory=list)
    temporaries: set[Path] = field(default_factory=set)
    placed: list[Path] = field(default_factory=list)

    def rollback(self) -> None:
        for path in self.temporaries:
            with suppress(OSError):
                os.unlink(path)
        for path in reversed(self.placed):
            with suppress(OSError):
                os.unlink(path)
        for path in reversed(self.directories):
            with suppress(OSError):
                os.rmdir(path)


def _member_mode(info: zipfile.ZipInfo) -> int:
    unix_mode = info.external_attr >> 16
    if stat.S_ISLNK(unix_mode):
        raise InvalidPackageError(
            "SYMLINK_NOT_ALLOWED", f"Entry {_shown(info.filename)} is a symbolic link"
        )
    if info.create_system == UNIX_SYSTEM and unix_mode & stat.S_IXUSR:
        return EXECUTABLE_MODE
    return FILE_MODE


def _reject_clashes(planned: Sequence[_Planned]) -> None:
    files = {item.entry.path for item in planned}
    for item in planned:
        for end in range(1, len(item.parts)):
            prefix = "/".join(item.parts[:end])
            if prefix in files:
                raise InvalidPackageError(
                    "DUPLICATE_PATH", f"{_shown(prefix)} is used both as a file and as a directory"
                )


def _plan_extraction(archive: zipfile.ZipFile, parsed: ParsedPackage) -> list[_Planned]:
    planned: list[_Planned] = []
    seen: set[str] = set()
    for entry in sorted(parsed.manifest, key=lambda item: item.path):
        parts = _checked_parts(entry.path)
        key = unicodedata.normalize("NFC", entry.path).casefold()
        if key in seen:
            raise InvalidPackageError(
                "DUPLICATE_PATH", f"The manifest lists {_shown(entry.path)} more than once"
            )
        seen.add(key)
        try:
            info = archive.getinfo(parsed.root_prefix + entry.path)
        except KeyError:
            raise CliError(
                f"{_shown(entry.path)} is in the manifest but missing from the archive",
                code="INTEGRITY_ERROR",
            ) from None
        planned.append(_Planned(entry, info, parts, _member_mode(info)))
    _reject_clashes(planned)
    return planned


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _require_plain_directory(state: os.stat_result, shown: str) -> None:
    if stat.S_ISLNK(state.st_mode):
        raise CliError(f"refusing to write through the symbolic link {_shown(shown)}")
    if not stat.S_ISDIR(state.st_mode):
        raise CliError(f"{_shown(shown)} exists and is not a directory")


def _locate_root(destination: Path) -> tuple[Path, list[Path]]:
    try:
        info = os.stat(destination)
    except FileNotFoundError:
        return Path(os.path.abspath(destination)), _missing_chain(destination)
    if not stat.S_ISDIR(info.st_mode):
        raise CliError(f"{_shown(destination)} exists and is not a directory")
    return Path(os.path.realpath(destination)), []


def _check_conflicts(root: Path, planned: Sequence[_Planned], *, force: bool) -> None:
    existing: list[str] = []
    for item in planned:
        current = root
        for position, part in enumerate(item.parts[:-1]):
            current = current / part
            state = _lstat(current)
            if state is None:
                break
            _require_plain_directory(state, "/".join(item.parts[: position + 1]))
        else:
            state = _lstat(current / item.parts[-1])
            if state is None:
                continue
            if stat.S_ISLNK(state.st_mode):
                raise CliError(f"refusing to replace the symbolic link {_shown(item.entry.path)}")
            if not stat.S_ISREG(state.st_mode):
                raise CliError(f"{_shown(item.entry.path)} exists and is not a regular file")
            existing.append(item.entry.path)
    if existing and not force:
        listed = ", ".join(_shown(path) for path in existing[:CONFLICTS_SHOWN])
        extra = len(existing) - CONFLICTS_SHOWN
        suffix = f" and {extra} more" if extra > 0 else ""
        raise CliError(
            f"{len(existing)} file(s) already exist: {listed}{suffix}; use --force to overwrite",
            code="CONFLICT",
        )


def _ensure_directory(root: Path, parts: Sequence[str], workspace: _Workspace) -> Path:
    current = root
    for position, part in enumerate(parts):
        current = current / part
        state = _lstat(current)
        if state is None:
            os.mkdir(current, DIRECTORY_MODE)
            workspace.directories.append(current)
            state = os.lstat(current)
        _require_plain_directory(state, "/".join(parts[: position + 1]))
    return current


def _write_member(archive: zipfile.ZipFile, item: _Planned, temporary: Path) -> None:
    descriptor = os.open(temporary, _CREATE_NEW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            digest = hashlib.sha256()
            written = 0
            try:
                with archive.open(item.info) as source:
                    for chunk in iter(lambda: source.read(CHUNK_BYTES), b""):
                        written += len(chunk)
                        if written > item.entry.size:
                            break
                        digest.update(chunk)
                        handle.write(chunk)
            except _READ_ERRORS as exc:
                raise CliError(
                    f"cannot read {_shown(item.entry.path)} from the package: {_clean(exc)}",
                    code="INTEGRITY_ERROR",
                ) from exc
            if written != item.entry.size or digest.hexdigest() != item.entry.sha256:
                raise CliError(
                    f"{_shown(item.entry.path)} does not match its manifest entry",
                    code="INTEGRITY_ERROR",
                )
            handle.flush()
            if hasattr(os, "fchmod"):
                os.fchmod(handle.fileno(), item.mode)
            else:
                os.chmod(temporary, item.mode)
    except BaseException:
        with suppress(OSError):
            os.unlink(temporary)
        raise


def _commit(
    staged: Sequence[tuple[Path, Path, _Planned]], workspace: _Workspace, *, force: bool
) -> None:
    for temporary, final, item in staged:
        state = _lstat(final)
        if state is not None:
            if stat.S_ISLNK(state.st_mode) or not stat.S_ISREG(state.st_mode):
                raise CliError(f"{_shown(item.entry.path)} is not a regular file")
            if not force:
                raise CliError(
                    f"{_shown(item.entry.path)} appeared while extracting; use --force to overwrite",
                    code="CONFLICT",
                )
        os.replace(temporary, final)
        workspace.temporaries.discard(temporary)
        if state is None:
            workspace.placed.append(final)


def _open_archive(data: bytes) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, *_READ_ERRORS) as exc:
        raise InvalidPackageError("CORRUPT_ARCHIVE", f"The archive is corrupt: {exc}") from exc


def extract_package(
    data: bytes, parsed: ParsedPackage, destination: Path, *, force: bool = False
) -> list[str]:
    with _open_archive(data) as archive:
        planned = _plan_extraction(archive, parsed)
        root, missing = _locate_root(destination)
        _check_conflicts(root, planned, force=force)
        workspace = _Workspace()
        try:
            for directory in missing:
                os.mkdir(directory, DIRECTORY_MODE)
                workspace.directories.append(directory)
            staged: list[tuple[Path, Path, _Planned]] = []
            for item in planned:
                folder = _ensure_directory(root, item.parts[:-1], workspace)
                final = folder / item.parts[-1]
                _assert_inside(root, final)
                temporary = folder / f".{secrets.token_hex(8)}.alx-tmp"
                _write_member(archive, item, temporary)
                workspace.temporaries.add(temporary)
                staged.append((temporary, final, item))
            _commit(staged, workspace, force=force)
        except OSError as exc:
            workspace.rollback()
            raise CliError(f"cannot extract into {_shown(destination)}: {_os_reason(exc)}") from exc
        except BaseException:
            workspace.rollback()
            raise
    return [item.entry.path for item in planned]


def parse_reference(spec: str) -> tuple[str, str]:
    name, separator, version = spec.partition("@")
    if not name:
        raise UsageError(f"invalid package reference {_shown(spec)}: the name is empty")
    valid_name = (
        _NAME_PATTERN.fullmatch(name) is not None and len(name) <= _MAX_NAME_LENGTH
    ) or looks_like_uuid(name)
    if not valid_name:
        raise UsageError(
            f"invalid package name {_shown(name)}: use lowercase letters, digits and hyphens"
        )
    if not separator:
        return name, LATEST
    if version != LATEST and not semver.is_valid(version):
        raise UsageError(
            f"invalid version {_shown(version)}: use MAJOR.MINOR.PATCH (semver) or '{LATEST}'"
        )
    return name, version


def _suggested_filename(header: str | None) -> str | None:
    match = _DISPOSITION_FILENAME.search(header or "")
    if match is None or _SAFE_FILENAME.fullmatch(match.group(1)) is None:
        return None
    return match.group(1)


def _resolved_version(filename: str | None, name: str, requested: str) -> str:
    match = _ARTIFACT_NAME.fullmatch(filename.removesuffix(".zip")) if filename else None
    if match is None or not semver.is_valid(match["version"]):
        return requested
    if match["name"] == name or looks_like_uuid(name):
        return match["version"]
    return requested


def _is_loopback(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def normalize_url(raw: str) -> str:
    text = raw.strip()
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError as exc:
        raise UsageError(f"invalid registry URL {_shown(raw)}: {_clean(exc)}") from exc
    if parts.scheme not in ("http", "https") or not parts.hostname or port == 0:
        raise UsageError(f"invalid registry URL {_shown(raw)}: use http:// or https:// and a host")
    if parts.username or parts.password:
        raise UsageError("the registry URL must not contain credentials; use --token instead")
    if parts.query or parts.fragment:
        raise UsageError(f"invalid registry URL {_shown(raw)}: remove the query and the fragment")
    return text.rstrip("/")


@contextmanager
def _network_errors(url: str) -> Iterator[None]:
    try:
        yield
    except httpx.TimeoutException as exc:
        raise CliError(f"the request to {_shown(url)} timed out", code="NETWORK_ERROR") from exc
    except httpx.ConnectError as exc:
        raise CliError(
            f"cannot connect to {_shown(url)}: {_clean(exc) or 'connection failed'}",
            code="NETWORK_ERROR",
        ) from exc
    except _NETWORK_ERRORS as exc:
        detail = _clean(exc) or type(exc).__name__
        raise CliError(
            f"network error talking to {_shown(url)}: {detail}", code="NETWORK_ERROR"
        ) from exc


def _json_body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except (ValueError, RecursionError):
        return None


def _bounded(response: httpx.Response) -> httpx.Response:
    body = bytearray()
    for chunk in response.iter_bytes(CHUNK_BYTES):
        body += chunk
        if len(body) > MAX_RESPONSE_BYTES:
            raise CliError(
                f"the server sent more than {MAX_RESPONSE_BYTES} bytes where a short answer "
                "was expected",
                code="TOO_LARGE",
            )
    return httpx.Response(response.status_code, headers=response.headers, content=bytes(body))


def _detail_message(detail: Any) -> str | None:
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list):
        messages = [str(item["msg"]) for item in detail if isinstance(item, dict) and "msg" in item]
        return "; ".join(messages) or None
    return None


def _failure(response: httpx.Response, *, has_token: bool) -> CliError:
    status = response.status_code
    if 300 <= status < 400:
        location = response.headers.get("Location")
        message = f"the server answered with a redirect (HTTP {status})"
        if location:
            message += f" to {_shown(location)}; use that address with --url"
        return CliError(message, code="REDIRECT", status=status)
    payload = _json_body(response)
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict) and isinstance(error.get("code"), str):
        message = error.get("message")
        reason = error.get("reason")
        failure = CliError(
            message if isinstance(message, str) and message else f"HTTP {status}",
            code=error["code"],
            reason=reason if isinstance(reason, str) and reason else None,
            status=status,
        )
    else:
        detail = _detail_message(payload.get("detail") if isinstance(payload, dict) else None)
        failure = CliError(
            detail or f"unexpected response from the server (HTTP {status})",
            code=f"HTTP_{status}",
            status=status,
        )
    if status == 401:
        failure.message += (
            "; check that the token is valid and has not expired"
            if has_token
            else f"; no token was sent, pass --token or set {TOKEN_ENV}"
        )
    return failure


class DownloadStream:
    def __init__(self, response: httpx.Response, url: str) -> None:
        declared = response.headers.get("X-Content-SHA256", "").strip()
        if _SHA256_PATTERN.fullmatch(declared) is None:
            raise CliError(
                "the server did not send a valid X-Content-SHA256 header; "
                "refusing to use an unverified download",
                code="INTEGRITY_ERROR",
            )
        self._response = response
        self._url = url
        self.expected = declared.lower()
        self.filename = _suggested_filename(response.headers.get("Content-Disposition"))

    def copy_to(self, sink: BinaryIO, max_bytes: int) -> Download:
        declared_length = self._response.headers.get("Content-Length", "")
        if declared_length.isdigit() and int(declared_length) > max_bytes:
            raise CliError(
                f"the server announces {declared_length} bytes, more than the {max_bytes} allowed",
                code="TOO_LARGE",
            )
        digest = hashlib.sha256()
        size = 0
        with _network_errors(self._url):
            for chunk in self._response.iter_bytes(CHUNK_BYTES):
                size += len(chunk)
                if size > max_bytes:
                    raise CliError(
                        f"the download exceeds the {max_bytes} bytes allowed; aborting",
                        code="TOO_LARGE",
                    )
                digest.update(chunk)
                sink.write(chunk)
        actual = digest.hexdigest()
        if actual != self.expected:
            raise CliError(
                f"sha256 mismatch: the server announced {self.expected} "
                f"but the content hashes to {actual}",
                code="INTEGRITY_ERROR",
            )
        return Download(sha256=actual, size=size, filename=self.filename)


class _AsyncBridge(httpx.BaseTransport):
    def __init__(self, transport: httpx.AsyncBaseTransport) -> None:
        self._transport = transport
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="alx-transport", daemon=True
        )
        self._thread.start()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        forwarded = httpx.Request(
            request.method,
            request.url,
            headers=request.headers,
            content=request.read(),
            extensions=request.extensions,
        )
        return asyncio.run_coroutine_threadsafe(self._exchange(forwarded), self._loop).result()

    async def _exchange(self, request: httpx.Request) -> httpx.Response:
        response = await self._transport.handle_async_request(request)
        content = await response.aread()
        await response.aclose()
        return httpx.Response(
            response.status_code, headers=response.headers, content=content, request=request
        )

    def close(self) -> None:
        asyncio.run_coroutine_threadsafe(self._transport.aclose(), self._loop).result()
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join()
        self._loop.close()


class RegistryClient:
    def __init__(self, remote: Remote, transport: Transport | None) -> None:
        if isinstance(transport, httpx.AsyncBaseTransport) and not isinstance(
            transport, httpx.BaseTransport
        ):
            transport = _AsyncBridge(transport)
        headers = {"User-Agent": f"alx/{_package_version()}"}
        if remote.token is not None:
            headers["Authorization"] = f"Bearer {remote.token}"
        self.base_url = remote.url
        self._has_token = remote.token is not None
        self._api = remote.url + API_PREFIX
        self._client = httpx.Client(
            headers=headers, transport=transport, timeout=REQUEST_TIMEOUT, follow_redirects=False
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._client.close()

    def _exchange(self, method: str, url: str, **options: Any) -> httpx.Response:
        with _network_errors(self.base_url), self._client.stream(method, url, **options) as raw:
            return _bounded(raw)

    def _expect(self, response: httpx.Response, *statuses: int) -> None:
        if response.status_code not in statuses:
            raise _failure(response, has_token=self._has_token)

    def _object(self, response: httpx.Response, *keys: str) -> dict[str, Any]:
        payload = _json_body(response)
        if not isinstance(payload, dict) or any(key not in payload for key in keys):
            raise CliError("the server returned an unexpected response", code="BAD_RESPONSE")
        return payload

    def upload(
        self, filename: str, data: bytes, *, version: str | None, publish: bool
    ) -> dict[str, Any]:
        fields: dict[str, str] = {}
        if version is not None:
            fields["version"] = version
        if publish:
            fields["publish"] = "true"
        response = self._exchange(
            "POST",
            f"{self._api}/packages",
            data=fields,
            files={"file": (filename, data, "application/zip")},
            headers={"Accept": "application/json"},
        )
        self._expect(response, 200, 201)
        payload = self._object(response, "skill", "version")
        if not isinstance(payload["skill"], dict) or not isinstance(payload["version"], dict):
            raise CliError("the server returned an unexpected response", code="BAD_RESPONSE")
        return payload

    def search(self, params: Sequence[tuple[str, str]]) -> dict[str, Any]:
        response = self._exchange(
            "GET",
            f"{self._api}/skills",
            params=list(params),
            headers={"Accept": "application/json"},
        )
        self._expect(response, 200)
        payload = self._object(response, "items")
        if not isinstance(payload["items"], list):
            raise CliError("the server returned an unexpected response", code="BAD_RESPONSE")
        return payload

    @contextmanager
    def open_download(self, ref: str, version: str) -> Iterator[DownloadStream]:
        url = (
            f"{self._api}/skills/{quote(ref, safe='')}/versions/{quote(version, safe='')}/download"
        )
        with (
            _network_errors(self.base_url),
            self._client.stream(
                "GET", url, headers={"Accept": "application/zip, application/json;q=0.5"}
            ) as response,
        ):
            if response.status_code != 200:
                raise _failure(_bounded(response), has_token=self._has_token)
            yield DownloadStream(response, self.base_url)


def _remote(args: argparse.Namespace, context: Context) -> Remote:
    url = normalize_url(args.url or context.env.get(URL_ENV) or DEFAULT_URL)
    token = args.token if args.token is not None else context.env.get(TOKEN_ENV)
    token = token.strip() if token is not None else None
    if token and _BEARER_CREDENTIAL.fullmatch(token) is None:
        raise UsageError("the credential contains characters that cannot be sent as a bearer token")
    return Remote(url=url, token=token or None)


def _open_client(remote: Remote, context: Context) -> RegistryClient:
    parts = urlsplit(remote.url)
    if (
        remote.token is not None
        and parts.scheme == "http"
        and not _is_loopback(parts.hostname or "")
    ):
        print(
            "warning: the token is sent over an unencrypted connection; use an https:// URL",
            file=sys.stderr,
        )
    return RegistryClient(remote, context.transport)


def _existing_path(raw: str) -> Path:
    if not raw.strip():
        raise UsageError("the path is empty")
    path = Path(raw)
    if not path.exists():
        raise UsageError(f"{_shown(raw)}: no such file or directory")
    return path


def _check_version_option(version: str | None) -> None:
    if version is None:
        return
    try:
        semver.parse(version)
    except ValidationError as exc:
        raise UsageError(f"invalid --version: {exc.message}") from exc


def _manifest_payload(parsed: ParsedPackage) -> list[dict[str, Any]]:
    return [entry.to_dict() for entry in parsed.manifest]


def _summary_payload(package: LoadedPackage) -> dict[str, Any]:
    parsed = package.parsed
    return {
        "kind": parsed.kind,
        "name": parsed.name,
        "version": parsed.version,
        "display_name": parsed.display_name,
        "description": parsed.description,
        "category": parsed.category,
        "tags": parsed.tags,
        "entrypoint": parsed.entrypoint,
        "file_count": parsed.file_count,
        "uncompressed_bytes": parsed.uncompressed_bytes,
        "size_bytes": parsed.size_bytes,
        "sha256": parsed.sha256,
    }


def _summary_rows(package: LoadedPackage) -> list[tuple[str, str]]:
    parsed = package.parsed
    return [
        ("kind", parsed.kind),
        ("name", parsed.name),
        ("version", parsed.version),
        ("display name", parsed.display_name),
        ("description", parsed.description),
        ("category", parsed.category or "-"),
        ("tags", ", ".join(parsed.tags) or "-"),
        ("entrypoint", parsed.entrypoint),
        (
            "files",
            f"{parsed.file_count} ({_human_bytes(parsed.uncompressed_bytes)} uncompressed)",
        ),
        ("package", f"{_human_bytes(parsed.size_bytes)}, sha256 {parsed.sha256}"),
    ]


def _cmd_validate(args: argparse.Namespace, context: Context) -> int:
    _check_version_option(args.version)
    path = _existing_path(args.path)
    try:
        package = load_package(path, version=args.version)
    except InvalidPackageError as exc:
        if args.json:
            _emit_json(
                {
                    "valid": False,
                    "path": args.path,
                    "errors": [{"reason": exc.reason, "message": exc.message}],
                }
            )
        else:
            print(f"INVALID: {_clean(args.path)}")
            print(f"  {exc.reason}: {_clean(exc.message)}")
        return EXIT_FAILURE
    if args.json:
        _emit_json(
            {
                "valid": True,
                "path": args.path,
                **_summary_payload(package),
                "files": _manifest_payload(package.parsed),
            }
        )
        return EXIT_OK
    print(f"OK: {_clean(args.path)}")
    _print_fields(_summary_rows(package))
    if args.files:
        print("  manifest:")
        for entry in package.parsed.manifest:
            print(f"    {_clean(entry.path)}  ({_human_bytes(entry.size)})")
    return EXIT_OK


def _pack_target(raw: str | None, package: LoadedPackage) -> Path:
    if raw is None:
        return Path(package.filename)
    target = Path(raw)
    if raw.endswith(("/", os.sep)) or target.is_dir():
        return target / package.filename
    return target


def _is_inside(directory: str, candidate: str) -> bool:
    return os.path.commonpath([directory, candidate]) == directory and candidate != directory


def _is_archive_file(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(4) in ZIP_SIGNATURES
    except OSError:
        return False


def _refuse_source_overwrite(real_source: str, target: Path) -> None:
    inside = _is_inside(real_source, os.path.realpath(target))
    if inside and target.is_file() and not _is_archive_file(target):
        raise CliError(
            f"refusing to overwrite {_shown(target)}: it is part of the package and "
            "is not an archive"
        )


def _cmd_pack(args: argparse.Namespace, context: Context) -> int:
    _check_version_option(args.version)
    if not args.directory.strip():
        raise UsageError("the directory is empty")
    source = Path(args.directory)
    if not source.is_dir():
        raise UsageError(f"{_shown(args.directory)} is not a directory")
    real_source = os.path.realpath(source)
    exclude: frozenset[str] = frozenset()
    if args.output and not args.output.endswith(("/", os.sep)) and not Path(args.output).is_dir():
        _refuse_source_overwrite(real_source, Path(args.output))
        exclude = frozenset({os.path.realpath(args.output)})
    package = pack_directory(source, version=args.version, exclude=exclude)
    target = _pack_target(args.output, package)
    real_target = os.path.realpath(target)
    if _is_inside(real_source, real_target):
        _refuse_source_overwrite(real_source, target)
        if os.path.lexists(target) and real_target not in exclude:
            package = pack_directory(source, version=args.version, exclude=exclude | {real_target})
        if os.path.relpath(real_target, real_source) != package.filename:
            print(
                f"warning: {_shown(target)} is inside the package directory and "
                "will be part of later packages unless it is removed",
                file=sys.stderr,
            )
    _write_file(target, package.data)
    if args.json:
        _emit_json({"path": str(target), **_summary_payload(package)})
        return EXIT_OK
    parsed = package.parsed
    print(f"Packed {_clean(target)}")
    _print_fields(
        [
            ("kind", parsed.kind),
            ("name", parsed.name),
            ("version", parsed.version),
            (
                "files",
                f"{parsed.file_count} ({_human_bytes(parsed.uncompressed_bytes)} uncompressed)",
            ),
            ("size", f"{_human_bytes(parsed.size_bytes)} ({parsed.size_bytes} bytes)"),
            ("sha256", parsed.sha256),
        ]
    )
    return EXIT_OK


def _cmd_publish(args: argparse.Namespace, context: Context) -> int:
    _check_version_option(args.version)
    remote = _remote(args, context)
    if remote.token is None:
        raise UsageError(f"publish needs a credential: pass --token or set {TOKEN_ENV}")
    path = _existing_path(args.path)
    package = load_package(path, version=args.version)
    with _open_client(remote, context) as client:
        payload = client.upload(
            package.filename, package.data, version=args.version, publish=args.publish
        )
    if args.json:
        _emit_json(payload)
        return EXIT_OK
    skill, version = payload["skill"], payload["version"]
    name = skill.get("name", package.parsed.name)
    number = version.get("version", package.parsed.version)
    status = version.get("status", "unknown")
    print(f"Uploaded {_clean(name)} {_clean(number)} ({_clean(skill.get('kind', 'unknown'))})")
    _print_fields(
        [
            ("status", str(status)),
            ("sha256", str(version.get("sha256", package.parsed.sha256))),
            (
                "size",
                f"{_human_bytes(package.parsed.size_bytes)}, {package.parsed.file_count} files",
            ),
            ("id", str(skill.get("id", "-"))),
        ]
    )
    if status == "draft":
        print("The version is a draft; upload with --publish or publish it through the API.")
    return EXIT_OK


def _description_cell(value: object) -> str:
    text = _clean(value)
    if len(text) > DESCRIPTION_COLUMN_WIDTH:
        return text[: DESCRIPTION_COLUMN_WIDTH - 3] + "..."
    return text


def _print_search(page: dict[str, Any], *, offset: int) -> None:
    items = [item for item in page["items"] if isinstance(item, dict)]
    if not items:
        print("No results.")
        return
    show_status = any(item.get("status", "published") != "published" for item in items)
    header = ["NAME", "KIND", "LATEST"] + (["STATUS"] if show_status else []) + ["DESCRIPTION"]
    rows = [
        [
            _clean(item.get("name", "?")),
            _clean(item.get("kind", "?")),
            _clean(item.get("latest_version") or "-"),
            *([_clean(item.get("status", "?"))] if show_status else []),
            _description_cell(item.get("description", "")),
        ]
        for item in items
    ]
    widths = [max(len(row[column]) for row in [header, *rows]) for column in range(len(header))]
    for row in [header, *rows]:
        print(
            "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        )
    total = page.get("total")
    total = total if isinstance(total, int) else len(items)
    print()
    print(f"{len(items)} of {total} result(s) shown")
    if offset + len(items) < total:
        print(f"More results are available: use --offset {offset + len(items)}")


def _cmd_search(args: argparse.Namespace, context: Context) -> int:
    remote = _remote(args, context)
    params: list[tuple[str, str]] = [("limit", str(args.limit)), ("offset", str(args.offset))]
    query = " ".join(args.query).strip()
    if query:
        params.append(("q", query))
    if args.kind:
        params.append(("kind", args.kind))
    params.extend(("tag", tag) for tag in args.tag)
    with _open_client(remote, context) as client:
        page = client.search(params)
    if args.json:
        _emit_json(page)
        return EXIT_OK
    _print_search(page, offset=args.offset)
    return EXIT_OK


def _download_target(output: str | None, filename: str) -> Path:
    if output is None:
        return Path(filename)
    target = Path(output)
    if output.endswith(("/", os.sep)) or target.is_dir():
        return target / filename
    return target


def _cmd_download(args: argparse.Namespace, context: Context) -> int:
    name, version = parse_reference(args.reference)
    remote = _remote(args, context)
    with _open_client(remote, context) as client, client.open_download(name, version) as stream:
        target = _download_target(args.output, stream.filename or f"{name}-{version}.zip")
        with _Part(target.parent) as part:
            result = stream.copy_to(part.handle, DEFAULT_LIMITS.max_package_bytes)
            part.commit(target)
    resolved = _resolved_version(result.filename, name, version)
    if args.json:
        _emit_json(
            {
                "name": name,
                "version": resolved,
                "path": str(target),
                "size_bytes": result.size,
                "sha256": result.sha256,
            }
        )
        return EXIT_OK
    print(f"Downloaded {_clean(name)} {_clean(resolved)} to {_clean(target)}")
    _print_fields(
        [("size", f"{_human_bytes(result.size)} ({result.size} bytes)"), ("sha256", result.sha256)]
    )
    return EXIT_OK


def _check_identity(parsed: ParsedPackage, name: str, version: str) -> None:
    if not looks_like_uuid(name) and parsed.name != name:
        raise CliError(
            f"the package declares the name {_shown(parsed.name)} but {_shown(name)} was requested",
            code="INTEGRITY_ERROR",
        )
    if version != LATEST and parsed.version != version:
        raise CliError(
            f"the package declares the version {_shown(parsed.version)} "
            f"but {_shown(version)} was requested",
            code="INTEGRITY_ERROR",
        )


def _cmd_pull(args: argparse.Namespace, context: Context) -> int:
    name, version = parse_reference(args.reference)
    remote = _remote(args, context)
    buffer = io.BytesIO()
    with _open_client(remote, context) as client, client.open_download(name, version) as stream:
        stream.copy_to(buffer, DEFAULT_LIMITS.max_package_bytes)
    data = buffer.getvalue()
    parsed = inspect_package(data, DEFAULT_LIMITS)
    _check_identity(parsed, name, version)
    destination = Path(args.dir) if args.dir else Path(parsed.name)
    files = extract_package(data, parsed, destination, force=args.force)
    if args.json:
        _emit_json(
            {
                "name": parsed.name,
                "version": parsed.version,
                "kind": parsed.kind,
                "directory": str(destination),
                "sha256": parsed.sha256,
                "files": files,
            }
        )
        return EXIT_OK
    print(
        f"Pulled {_clean(parsed.name)} {_clean(parsed.version)} ({parsed.kind}) "
        f"into {_clean(destination)}"
    )
    _print_fields(
        [
            ("files", f"{len(files)} ({_human_bytes(parsed.uncompressed_bytes)})"),
            ("sha256", parsed.sha256),
        ]
    )
    for path in files[:LISTED_FILES]:
        print(f"    {_clean(path)}")
    if len(files) > LISTED_FILES:
        print(f"    ... and {len(files) - LISTED_FILES} more")
    return EXIT_OK


class _Parser(argparse.ArgumentParser):
    def exit(self, status: int = 0, message: str | None = None):
        if message:
            self._print_message(message, sys.stderr)
        raise _ParserExit(status)


def _bounded_int(low: int, high: int | None):
    def convert(raw: str) -> int:
        try:
            value = int(raw)
        except ValueError:
            raise argparse.ArgumentTypeError(f"invalid integer: {raw!r}") from None
        if value < low or (high is not None and value > high):
            bound = f"at least {low}" if high is None else f"between {low} and {high}"
            raise argparse.ArgumentTypeError(f"must be {bound}")
        return value

    return convert


def _lowercase_tag(raw: str) -> str:
    return raw.strip().lower()


def _remote_options() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parent.add_argument(
        "--url", metavar="URL", help=f"registry address (default: ${URL_ENV} or {DEFAULT_URL})"
    )
    parent.add_argument(
        "--token", metavar="TOKEN", help=f"bearer credential (default: ${TOKEN_ENV})"
    )
    return parent


def _json_option() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parent.add_argument("--json", action="store_true", help="print machine-readable JSON")
    return parent


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="alx",
        description="Validate, pack, publish and consume Alexandria packages.",
        epilog=(
            "environment:\n"
            f"  {URL_ENV}    registry address (default: {DEFAULT_URL})\n"
            f"  {TOKEN_ENV}  bearer credential: a JWT or an alx_ API token\n"
            f"  {DEBUG_ENV}  show the traceback of unexpected failures\n"
            "\n"
            "exit status:\n"
            "  0  success\n"
            "  1  failure: invalid package, server error, network error or integrity error\n"
            "  2  usage error"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    parser.add_argument("-V", "--version", action="version", version=f"alx {_package_version()}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)
    remote, as_json = _remote_options(), _json_option()

    validate = commands.add_parser(
        "validate",
        parents=[as_json],
        allow_abbrev=False,
        help="check a package directory or .zip against the package contract",
        description=(
            "Runs the same inspector as the registry. Exits 0 when the package is valid and 1 "
            "when it is not, printing the reason."
        ),
    )
    validate.add_argument("path", help="package directory or .zip file")
    validate.add_argument(
        "--version", metavar="VERSION", help="version to assume when the frontmatter has none"
    )
    validate.add_argument("--files", action="store_true", help="list the files of the package")
    validate.set_defaults(handler=_cmd_validate)

    pack = commands.add_parser(
        "pack",
        parents=[as_json],
        allow_abbrev=False,
        help="build a deterministic .zip from a package directory",
        description=(
            "Validates the directory and then writes a .zip whose bytes depend only on the "
            "content of the files. Nothing is written when the package is invalid."
        ),
    )
    pack.add_argument("directory", help="package directory")
    pack.add_argument(
        "-o",
        "--output",
        metavar="FILE",
        help="output file or directory (default: NAME-VERSION.zip)",
    )
    pack.add_argument(
        "--version", metavar="VERSION", help="version to assume when the frontmatter has none"
    )
    pack.set_defaults(handler=_cmd_pack)

    publish = commands.add_parser(
        "publish",
        parents=[remote, as_json],
        allow_abbrev=False,
        help="upload a package directory or .zip to a registry",
        description=(
            "Validates the package locally, packs it when it is a directory and uploads it. "
            "Needs a credential with write scope."
        ),
    )
    publish.add_argument("path", help="package directory or .zip file")
    publish.add_argument("--version", metavar="VERSION", help="version to send with the upload")
    publish.add_argument(
        "--publish", action="store_true", help="publish the version instead of leaving a draft"
    )
    publish.set_defaults(handler=_cmd_publish)

    search = commands.add_parser(
        "search",
        parents=[remote, as_json],
        allow_abbrev=False,
        help="search the catalog of a registry",
        description="Lists the entries that match every word and every filter.",
    )
    search.add_argument("query", nargs="*", help="words to look for (default: list everything)")
    search.add_argument("--kind", choices=("skill", "grounding"), help="only this kind of entry")
    search.add_argument(
        "--tag",
        action="append",
        default=[],
        type=_lowercase_tag,
        metavar="TAG",
        help="require this tag (repeatable)",
    )
    search.add_argument(
        "--limit",
        type=_bounded_int(1, MAX_SEARCH_LIMIT),
        default=DEFAULT_SEARCH_LIMIT,
        help=f"results per page, 1 to {MAX_SEARCH_LIMIT} (default: {DEFAULT_SEARCH_LIMIT})",
    )
    search.add_argument(
        "--offset", type=_bounded_int(0, None), default=0, help="results to skip (default: 0)"
    )
    search.set_defaults(handler=_cmd_search)

    download = commands.add_parser(
        "download",
        parents=[remote, as_json],
        allow_abbrev=False,
        help="download a package .zip and verify its sha256",
        description=(
            "Saves NAME[@VERSION] (default: latest) and checks it against the sha256 announced "
            "in X-Content-SHA256. The file is kept only when the hashes match."
        ),
    )
    download.add_argument("reference", metavar="NAME[@VERSION]", help="package to download")
    download.add_argument(
        "-o",
        "--output",
        metavar="FILE",
        help="output file or directory (default: NAME-VERSION.zip)",
    )
    download.set_defaults(handler=_cmd_download)

    pull = commands.add_parser(
        "pull",
        parents=[remote, as_json],
        allow_abbrev=False,
        help="download a package, revalidate it and extract it safely",
        description=(
            "Downloads NAME[@VERSION], verifies its sha256, validates the archive again and "
            "extracts only the files of its manifest. It never writes outside the destination, "
            "never follows symbolic links and never overwrites an existing file unless --force "
            "is given."
        ),
    )
    pull.add_argument("reference", metavar="NAME[@VERSION]", help="package to pull")
    pull.add_argument("-d", "--dir", metavar="DIR", help="destination directory (default: ./NAME)")
    pull.add_argument("--force", action="store_true", help="overwrite files that already exist")
    pull.set_defaults(handler=_cmd_pull)
    return parser


def _run(argv: Sequence[str], context: Context) -> int:
    for stream in (sys.stdout, sys.stderr):
        with suppress(AttributeError, ValueError, OSError):
            stream.reconfigure(errors="replace")
    try:
        args = build_parser().parse_args(argv)
    except _ParserExit as request:
        return request.status
    as_json = bool(getattr(args, "json", False))
    try:
        return args.handler(args, context)
    except UsageError as exc:
        print(f"alx {args.command}: error: {_clean(exc.message)}", file=sys.stderr)
        return EXIT_USAGE
    except CliError as exc:
        _print_error(exc, as_json=as_json)
        return EXIT_FAILURE
    except InvalidPackageError as exc:
        _print_error(_invalid_package(exc), as_json=as_json)
        return EXIT_FAILURE
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED
    except BrokenPipeError:
        return EXIT_FAILURE
    except OSError as exc:
        location = f": {_shown(exc.filename)}" if exc.filename else ""
        _print_error(CliError(f"{_os_reason(exc)}{location}"), as_json=as_json)
        return EXIT_FAILURE
    except Exception as exc:
        if context.env.get(DEBUG_ENV):
            raise
        message = f"unexpected failure ({type(exc).__name__}): {_clean(exc)}"
        _print_error(CliError(message, code="INTERNAL_ERROR"), as_json=as_json)
        return EXIT_FAILURE


def main(
    argv: Sequence[str] | None = None,
    *,
    transport: Transport | None = None,
    env: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    context = Context(env=os.environ if env is None else env, transport=transport)
    arguments = list(sys.argv[1:] if argv is None else argv)
    with redirect_stdout(out), redirect_stderr(err):
        return _run(arguments, context)


if __name__ == "__main__":
    raise SystemExit(main())
