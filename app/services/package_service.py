from __future__ import annotations

import asyncio
import codecs
import hashlib
import sys
import threading
import weakref
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Hashable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, BinaryIO

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import LimitsSettings, settings
from app.logging import get_logger
from app.models.schemas import (
    DownloadInfo,
    EntrypointContent,
    FileContent,
    FileListing,
    ManifestEntryResponse,
    UploadResult,
)
from app.models.skill import Skill, SkillKind, SkillVersion, VersionStatus
from app.repositories import skill_repo
from app.services import access, catalog, mappers
from app.services.errors import (
    ConflictError,
    ForbiddenError,
    InvalidPackageError,
    NotFoundError,
    PayloadTooLargeError,
    StorageError,
    ValidationError,
)
from app.services.package_inspector import (
    PackageLimits,
    ParsedPackage,
    inspect_package,
    open_package_member,
    validate_requested_path,
)
from app.services.principal import Principal
from app.services.transactions import write_transaction
from app.storage import BlobStore, BlobStoreError

logger = get_logger("app.services.package")

UPLOAD_ATTEMPTS = 2
RAW_BATCH_BYTES = 256 * 1024
CONTENT_CACHE_BYTES = 32 * 1024 * 1024
CACHE_ENTRY_OVERHEAD = 512
MANIFEST_COST_FACTOR = 10
MANIFEST_KEY = "manifest"
TEXT_KEY = "text"


@dataclass(frozen=True)
class RawFile:
    path: str
    media_type: str
    text: bool
    size: int
    stream: AsyncIterator[bytes]


@dataclass(frozen=True)
class PackageDownload:
    info: DownloadInfo
    filename: str
    stream: AsyncIterator[bytes]
    pinned: bool = True


def limits_from_settings(limits: LimitsSettings) -> PackageLimits:
    return PackageLimits(
        max_package_bytes=limits.max_package_bytes,
        max_uncompressed_bytes=limits.max_uncompressed_bytes,
        max_file_bytes=limits.max_file_bytes,
        max_files=limits.max_files,
    )


def base64_length(size: int) -> int:
    return 4 * ((size + 2) // 3)


def encoded_package_limit(limits: PackageLimits | None = None) -> int:
    active = limits if limits is not None else limits_from_settings(settings.registry.limits)
    return base64_length(active.max_package_bytes)


class ContentCache:
    def __init__(self, max_bytes: int = CONTENT_CACHE_BYTES) -> None:
        self.max_bytes = max(0, max_bytes)
        self._items: OrderedDict[Hashable, tuple[object, int]] = OrderedDict()
        self._total = 0
        self._lock = threading.Lock()

    @property
    def total_bytes(self) -> int:
        return self._total

    def __len__(self) -> int:
        return len(self._items)

    def get(self, key: Hashable) -> object | None:
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            self._items.move_to_end(key)
            return item[0]

    def put(self, key: Hashable, value: object, cost: int) -> None:
        charged = cost + CACHE_ENTRY_OVERHEAD
        with self._lock:
            previous = self._items.pop(key, None)
            if previous is not None:
                self._total -= previous[1]
            if charged > self.max_bytes:
                return
            self._items[key] = (value, charged)
            self._total += charged
            while self._total > self.max_bytes:
                _, (_, dropped) = self._items.popitem(last=False)
                self._total -= dropped

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._total = 0


_STORE_CACHES: weakref.WeakKeyDictionary[Any, ContentCache] = weakref.WeakKeyDictionary()
_STORE_CACHES_LOCK = threading.Lock()


def shared_cache(store: BlobStore) -> ContentCache:
    try:
        with _STORE_CACHES_LOCK:
            cache = _STORE_CACHES.get(store)
            if cache is None:
                cache = _STORE_CACHES[store] = ContentCache()
            return cache
    except TypeError:
        return ContentCache(0)


@dataclass(frozen=True)
class _StoredFile:
    version_id: str
    sha256: str
    root_prefix: str
    entry: ManifestEntryResponse


class _Fingerprint:
    __slots__ = ("digest", "size")

    def __init__(self) -> None:
        self.digest = hashlib.sha256()
        self.size = 0

    def feed(self, chunk: bytes) -> None:
        self.digest.update(chunk)
        self.size += len(chunk)


def _extract_member(
    reader: BinaryIO, root_prefix: str, entry: ManifestEntryResponse, limit: int | None
) -> tuple[bytes, bool]:
    with open_package_member(reader, root_prefix, entry.path) as member:
        data = member.read(entry.size if limit is None else limit, truncate=limit is not None)
    if limit is None:
        intact = len(data) == entry.size and hashlib.sha256(data).hexdigest() == entry.sha256
    else:
        intact = len(data) == limit
    return data, intact


def _read_batch(chunks: Iterator[bytes], fingerprint: _Fingerprint) -> list[bytes]:
    batch: list[bytes] = []
    size = 0
    for chunk in chunks:
        fingerprint.feed(chunk)
        batch.append(chunk)
        size += len(chunk)
        if size >= RAW_BATCH_BYTES:
            break
    return batch


def _decode(data: bytes, *, complete: bool) -> str:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    return decoder.decode(data, final=complete)


class PackageService:
    def __init__(
        self,
        db: AsyncSession,
        store: BlobStore,
        *,
        limits: PackageLimits | None = None,
        public_read: bool | None = None,
        auto_publish: bool | None = None,
        max_inline_read_bytes: int | None = None,
        public_url: str | None = None,
        cache: ContentCache | None = None,
    ) -> None:
        registry = settings.registry
        self.db = db
        self.store = store
        self.cache = shared_cache(store) if cache is None else cache
        self.limits = limits if limits is not None else limits_from_settings(registry.limits)
        self.public_read = registry.public_read if public_read is None else public_read
        self.auto_publish = registry.auto_publish if auto_publish is None else auto_publish
        self.max_inline_read_bytes = (
            registry.limits.max_inline_read_bytes
            if max_inline_read_bytes is None
            else max_inline_read_bytes
        )
        base_url = settings.server.public_url if public_url is None else public_url
        self.public_url = base_url.rstrip("/")

    async def upload(
        self,
        principal: Principal | None,
        data: bytes,
        *,
        version: str | None = None,
        publish: bool | None = None,
    ) -> UploadResult:
        writer = access.require_write(principal)
        payload = self._payload(data)
        if publish is not None and not isinstance(publish, bool):
            raise ValidationError("publish must be a boolean")
        if len(payload) > self.limits.max_package_bytes:
            self._reject(
                "payload too large",
                writer,
                size_bytes=len(payload),
                limit=self.limits.max_package_bytes,
            )
            raise PayloadTooLargeError(
                f"The package exceeds the limit of {self.limits.max_package_bytes} bytes"
            )
        try:
            parsed = await asyncio.to_thread(inspect_package, payload, self.limits, version=version)
        except InvalidPackageError as exc:
            self._reject("invalid package", writer, reason=exc.reason)
            raise
        publish_now = self.auto_publish if publish is None else publish
        result = await self._persist(writer, parsed, payload, publish_now)
        logger.info(
            f"Package uploaded: {parsed.name} {parsed.version}",
            extra={
                "extra_data": {
                    "skill_id": result.skill.id,
                    "version_id": result.version.id,
                    "name": parsed.name,
                    "version": parsed.version,
                    "kind": parsed.kind,
                    "status": result.version.status,
                    "sha256": parsed.sha256,
                    "size_bytes": parsed.size_bytes,
                    "user_id": writer.user_id,
                }
            },
        )
        return result

    async def _persist(
        self, writer: Principal, parsed: ParsedPackage, payload: bytes, publish_now: bool
    ) -> UploadResult:
        conflict: IntegrityError | None = None
        for _ in range(UPLOAD_ATTEMPTS):
            skill = await skill_repo.get_skill_by_name(self.db, parsed.name)
            if skill is not None:
                await self._check_existing(writer, skill, parsed)
            await self._store_blob(parsed.sha256, payload)
            try:
                async with write_transaction(self.db):
                    return await self._insert(writer, skill, parsed, publish_now)
            except IntegrityError as exc:
                conflict = exc
                self._reject(
                    "concurrent change",
                    writer,
                    name=parsed.name,
                    version=parsed.version,
                    sha256=parsed.sha256,
                )
        raise ConflictError(
            f"Version {parsed.version} of '{parsed.name}' conflicts with a concurrent "
            "change; check the current state and retry"
        ) from conflict

    async def manifest(
        self, principal: Principal | None, ref: str, version: str = catalog.LATEST
    ) -> FileListing:
        skill, target = await self._resolve(principal, ref, version)
        return FileListing(
            name=skill.name, version=target.version, files=list(self._entries(target))
        )

    async def entrypoint(
        self, principal: Principal | None, ref: str, version: str = catalog.LATEST
    ) -> EntrypointContent:
        skill, target = await self._resolve(principal, ref, version)
        content = await self._file_content(target, target.entrypoint)
        return EntrypointContent(
            **content.model_dump(),
            name=skill.name,
            version=target.version,
            kind=SkillKind(skill.kind).value,
        )

    async def read_file(
        self, principal: Principal | None, ref: str, version: str, path: str
    ) -> FileContent:
        _, target = await self._resolve(principal, ref, version)
        return await self._file_content(target, path)

    async def open_raw(
        self, principal: Principal | None, ref: str, version: str, path: str
    ) -> RawFile:
        _, target = await self._resolve(principal, ref, version)
        entry = self._locate(target, path)
        stored = _StoredFile(target.id, target.blob_sha256, target.root_prefix, entry)
        verification = self._member_chunks(stored)
        try:
            async for _ in verification:
                pass
        finally:
            await verification.aclose()
        return RawFile(
            path=entry.path,
            media_type=entry.media_type,
            text=entry.text,
            size=entry.size,
            stream=self._member_chunks(stored),
        )

    async def download_info(
        self, principal: Principal | None, ref: str, version: str = catalog.LATEST
    ) -> DownloadInfo:
        skill, target = await self._resolve(principal, ref, version)
        return self._download_info(skill, target)

    async def open_download(
        self, principal: Principal | None, ref: str, version: str = catalog.LATEST
    ) -> PackageDownload:
        skill, target = await self._resolve(principal, ref, version)
        info = self._download_info(skill, target)
        await self._check_blob_size(target)
        return PackageDownload(
            info=info,
            filename=f"{skill.name}-{target.version}.zip",
            stream=self._stream(target.id, target.blob_sha256, target.size_bytes),
            pinned=version != catalog.LATEST,
        )

    @staticmethod
    def _payload(data: object) -> bytes:
        if isinstance(data, bytes):
            return data
        if isinstance(data, bytearray | memoryview):
            return bytes(data)
        raise ValidationError("The package content must be bytes")

    async def _check_existing(self, writer: Principal, skill: Skill, parsed: ParsedPackage) -> None:
        if not access.is_owner_or_admin(writer, skill.owner_id):
            self._reject("entry belongs to another user", writer, name=parsed.name)
            raise ForbiddenError(f"'{parsed.name}' belongs to another user")
        if SkillKind(skill.kind).value != parsed.kind:
            existing_kind = SkillKind(skill.kind).value
            self._reject("kind change", writer, name=parsed.name, kind=parsed.kind)
            raise ConflictError(
                f"'{parsed.name}' is a {existing_kind} and cannot receive a {parsed.kind} package"
            )
        if await skill_repo.get_version(self.db, skill.id, parsed.version) is not None:
            self._reject("duplicate version", writer, name=parsed.name, version=parsed.version)
            raise ConflictError(f"Version {parsed.version} of '{parsed.name}' already exists")

    async def _store_blob(self, sha256: str, payload: bytes) -> None:
        try:
            await self.store.put(sha256, payload)
        except BlobStoreError as exc:
            logger.error(
                f"Package blob could not be stored: {exc}",
                extra={"extra_data": {"sha256": sha256}},
            )
            raise StorageError("The package could not be stored") from exc

    async def _insert(
        self, writer: Principal, skill: Skill | None, parsed: ParsedPackage, publish_now: bool
    ) -> UploadResult:
        if skill is None:
            skill = await skill_repo.create_skill(
                self.db,
                name=parsed.name,
                display_name=parsed.display_name,
                description=parsed.description,
                kind=SkillKind(parsed.kind),
                owner_id=writer.user_id,
                category=parsed.category,
                search_text=parsed.search_text,
                tags=parsed.tags,
            )
        else:
            await skill_repo.lock_skill(self.db, skill.id)
        created = await skill_repo.create_version(
            self.db,
            skill_id=skill.id,
            version=parsed.version,
            blob_sha256=parsed.sha256,
            size_bytes=parsed.size_bytes,
            uncompressed_bytes=parsed.uncompressed_bytes,
            file_count=parsed.file_count,
            entrypoint=parsed.entrypoint,
            root_prefix=parsed.root_prefix,
            display_name=parsed.display_name,
            description=parsed.description,
            category=parsed.category,
            tags=parsed.tags,
            metadata=parsed.metadata,
            manifest=[entry.to_dict() for entry in parsed.manifest],
            search_text=parsed.search_text,
            uploaded_by=writer.user_id,
            status=VersionStatus.PUBLISHED if publish_now else VersionStatus.DRAFT,
            published_by=writer.user_id if publish_now else None,
            published_at=datetime.now(UTC) if publish_now else None,
        )
        await catalog.refresh_skill(self.db, skill)
        return UploadResult(
            skill=mappers.skill_summary(skill), version=mappers.version_summary(created)
        )

    async def _resolve(
        self, principal: Principal | None, ref: str, version: str
    ) -> tuple[Skill, SkillVersion]:
        access.require_read(principal, public_read=self.public_read)
        skill = await catalog.get_visible_skill(self.db, principal, ref)
        target = await catalog.resolve_version(self.db, principal, skill, version)
        return skill, target

    def _entries(self, target: SkillVersion) -> tuple[ManifestEntryResponse, ...]:
        raw = target.manifest
        if not isinstance(raw, str):
            return tuple(mappers.manifest_entries(target))
        key = (MANIFEST_KEY, raw)
        cached = self.cache.get(key)
        if isinstance(cached, tuple):
            return cached
        entries = tuple(mappers.manifest_entries(target))
        self.cache.put(key, entries, len(raw) * MANIFEST_COST_FACTOR)
        return entries

    def _locate(self, target: SkillVersion, path: str) -> ManifestEntryResponse:
        checked = validate_requested_path(path)
        for entry in self._entries(target):
            if entry.path == checked:
                return entry
        raise NotFoundError(f"File not found: {checked}")

    async def _file_content(self, target: SkillVersion, path: str) -> FileContent:
        entry = self._locate(target, path)
        if not entry.text:
            return FileContent(
                path=entry.path,
                size=entry.size,
                sha256=entry.sha256,
                media_type=entry.media_type,
                text=False,
                content=None,
                truncated=False,
            )
        truncated = entry.size > self.max_inline_read_bytes
        limit = self.max_inline_read_bytes if truncated else None
        key = (
            TEXT_KEY,
            target.blob_sha256,
            target.root_prefix,
            entry.path,
            entry.size,
            entry.sha256,
            limit,
        )
        content = self.cache.get(key)
        if not isinstance(content, str):
            data = await self._read_member(target, entry, limit=limit)
            content = _decode(data, complete=not truncated)
            self.cache.put(key, content, sys.getsizeof(content))
        return FileContent(
            path=entry.path,
            size=entry.size,
            sha256=entry.sha256,
            media_type=entry.media_type,
            text=True,
            content=content,
            truncated=truncated,
        )

    async def _open_blob(self, stored: _StoredFile) -> BinaryIO:
        try:
            return await self.store.open(stored.sha256)
        except BlobStoreError as exc:
            self._log_corrupt(
                stored.version_id, stored.sha256, f"the stored package is unavailable: {exc}"
            )
            raise StorageError("The stored package for this version is unavailable") from exc

    async def _in_thread(
        self, stored: _StoredFile, function: Callable[..., Any], *arguments: Any
    ) -> Any:
        try:
            return await asyncio.to_thread(function, *arguments)
        except ValueError as exc:
            self._log_corrupt(
                stored.version_id,
                stored.sha256,
                f"{stored.entry.path} cannot be read from the package: {exc}",
            )
            raise StorageError("The stored package cannot be read") from exc

    def _mismatch(self, stored: _StoredFile) -> StorageError:
        self._log_corrupt(
            stored.version_id,
            stored.sha256,
            f"{stored.entry.path} does not match its manifest entry",
        )
        return StorageError("The stored file does not match its manifest entry")

    async def _read_member(
        self, target: SkillVersion, entry: ManifestEntryResponse, *, limit: int | None = None
    ) -> bytes:
        stored = _StoredFile(target.id, target.blob_sha256, target.root_prefix, entry)
        reader = await self._open_blob(stored)
        try:
            data, intact = await self._in_thread(
                stored, _extract_member, reader, stored.root_prefix, entry, limit
            )
        finally:
            reader.close()
        if not intact:
            raise self._mismatch(stored)
        return data

    async def _member_chunks(self, stored: _StoredFile) -> AsyncIterator[bytes]:
        reader = await self._open_blob(stored)
        fingerprint = _Fingerprint()
        try:
            member = await self._in_thread(
                stored, open_package_member, reader, stored.root_prefix, stored.entry.path
            )
            try:
                chunks = member.chunks()
                while True:
                    batch = await self._in_thread(stored, _read_batch, chunks, fingerprint)
                    if not batch or fingerprint.size > stored.entry.size:
                        break
                    for chunk in batch:
                        yield chunk
            finally:
                member.close()
        finally:
            reader.close()
        if (
            fingerprint.size != stored.entry.size
            or fingerprint.digest.hexdigest() != stored.entry.sha256
        ):
            raise self._mismatch(stored)

    def _download_info(self, skill: Skill, target: SkillVersion) -> DownloadInfo:
        public = (
            self.public_read
            and access.can_view_skill(None, skill)
            and access.can_view_version(None, skill, target)
        )
        return DownloadInfo(
            name=skill.name,
            version=target.version,
            sha256=target.blob_sha256,
            size_bytes=target.size_bytes,
            url=mappers.download_url(self.public_url, skill.name, target.version),
            requires_auth=not public,
        )

    async def _check_blob_size(self, target: SkillVersion) -> None:
        try:
            size = await self.store.size(target.blob_sha256)
        except BlobStoreError as exc:
            self._storage_failure(target, f"the stored package is unavailable: {exc}")
            raise StorageError("The stored package for this version is unavailable") from exc
        if size != target.size_bytes:
            self._storage_failure(
                target, f"the stored package has {size} bytes, expected {target.size_bytes}"
            )
            raise StorageError("The stored package does not match its recorded size")

    async def _stream(self, version_id: str, sha256: str, size: int) -> AsyncIterator[bytes]:
        source = None
        digest = hashlib.sha256()
        total = 0
        try:
            source = self.store.stream(sha256)
            async for chunk in source:
                digest.update(chunk)
                total += len(chunk)
                yield chunk
        except BlobStoreError as exc:
            self._log_corrupt(version_id, sha256, f"the stored package cannot be read: {exc}")
            raise StorageError("The stored package for this version is unavailable") from exc
        finally:
            close = getattr(source, "aclose", None)
            if close is not None:
                await close()
        if total != size or digest.hexdigest() != sha256:
            self._log_corrupt(version_id, sha256, "the stored package does not match its hash")
            raise StorageError("The stored package does not match its recorded hash")

    @staticmethod
    def _log_corrupt(version_id: str, sha256: str, detail: str) -> None:
        logger.error(
            f"Stored package failure for version {version_id}: {detail}",
            extra={"extra_data": {"version_id": version_id, "sha256": sha256}},
        )

    def _storage_failure(self, target: SkillVersion, detail: str) -> None:
        self._log_corrupt(target.id, target.blob_sha256, detail)

    @staticmethod
    def _reject(summary: str, writer: Principal, **data: object) -> None:
        logger.warning(
            f"Upload rejected: {summary}",
            extra={"extra_data": {"user_id": writer.user_id, **data}},
        )
