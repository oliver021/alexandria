from __future__ import annotations

import asyncio
import base64
import dataclasses
import hashlib
import io
import json
import logging
import random
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import BinaryIO

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config.settings import LimitsSettings, settings
from app.db.session import configure_sqlite_engine
from app.models import Base, Skill, SkillStatus, SkillVersion, TokenScope, VersionStatus
from app.models.schemas import (
    DownloadInfo,
    EntrypointContent,
    FileContent,
    FileListing,
    UploadResult,
)
from app.repositories import skill_repo
from app.services import package_service as package_service_module
from app.services.errors import (
    ConflictError,
    ForbiddenError,
    InvalidPackageError,
    NotFoundError,
    PayloadTooLargeError,
    StorageError,
    UnauthorizedError,
    ValidationError,
)
from app.services.package_inspector import DEFAULT_LIMITS, PackageLimits, inspect_package
from app.services.package_service import (
    PackageDownload,
    PackageService,
    RawFile,
    base64_length,
    encoded_package_limit,
    limits_from_settings,
)
from app.services.semver import InvalidVersionError
from app.services.skill_service import SkillService
from app.services.version_service import VersionService
from app.storage import BlobStoreError, LocalBlobStore
from tests.helpers.factories import (
    ACTOR_NAMES,
    PRIVILEGED_ACTORS,
    add_skill,
    add_version,
    make_actors,
    make_principal,
    upload_package,
)
from tests.helpers.packages import (
    corrupt_entry_data,
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

PUBLIC_URL = "https://registry.example.com"
RANDOM_BLOB = random.Random(20240601).randbytes(300 * 1024)

FILES = {
    "docs/guide.md": "# Guide\n\nHello there\n",
    "scripts/run.py": "print('hi')\n",
    "data/table.bin": bytes(range(256)),
    "notes/ünïcode.txt": "héllo wörld\n",
    "empty.txt": "",
    "big.txt": "line\n" * 400,
    "wide.txt": "é" * 200,
}


class RecordingStore:
    def __init__(
        self, inner: LocalBlobStore, *, chunk_size: int = 4096, fail_after: int | None = None
    ):
        self.inner = inner
        self.chunk_size = chunk_size
        self.fail_after = fail_after
        self.opened: list[str] = []
        self.closed: list[str] = []
        self.handles: list[BinaryIO] = []

    async def put(self, key: str, data: bytes) -> bool:
        return await self.inner.put(key, data)

    async def get(self, key: str) -> bytes:
        return await self.inner.get(key)

    async def open(self, key: str) -> BinaryIO:
        handle = await self.inner.open(key)
        self.handles.append(handle)
        return handle

    async def exists(self, key: str) -> bool:
        return await self.inner.exists(key)

    async def delete(self, key: str) -> bool:
        return await self.inner.delete(key)

    async def size(self, key: str) -> int:
        return await self.inner.size(key)

    def stream(self, key: str, chunk_size: int = 0) -> AsyncIterator[bytes]:
        return self._chunks(key)

    async def _chunks(self, key: str) -> AsyncIterator[bytes]:
        self.opened.append(key)
        try:
            data = await self.inner.get(key)
            for number, start in enumerate(range(0, len(data), self.chunk_size)):
                if self.fail_after is not None and number >= self.fail_after:
                    raise BlobStoreError("the disk went away")
                yield data[start : start + self.chunk_size]
        finally:
            self.closed.append(key)


def sha(content: bytes | str) -> str:
    raw = content.encode("utf-8") if isinstance(content, str) else content
    return hashlib.sha256(raw).hexdigest()


def zip_member(data: bytes, name: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return archive.read(name)


def blob_path(store: LocalBlobStore, digest: str) -> Path:
    return store.root / digest[:2] / f"{digest}.zip"


def stored_blobs(store: LocalBlobStore) -> list[str]:
    return sorted(path.stem for path in store.root.rglob("*.zip")) if store.root.exists() else []


async def collect(stream) -> list[bytes]:
    return [chunk async for chunk in stream]


async def fails(awaitable, error: type[Exception] = StorageError) -> Exception:
    with pytest.raises(error) as raised:
        await awaitable
    return raised.value


async def row_counts(session_factory) -> tuple[int, int]:
    async with session_factory() as session:
        skills = (await session.execute(select(func.count()).select_from(Skill))).scalar_one()
        versions = (
            await session.execute(select(func.count()).select_from(SkillVersion))
        ).scalar_one()
    return skills, versions


async def stored_version(session_factory, name: str, version: str) -> SkillVersion | None:
    async with session_factory() as session:
        skill = await skill_repo.get_skill_by_name(session, name)
        if skill is None:
            return None
        return await skill_repo.get_version(session, skill.id, version)


@pytest.fixture
def service(db_session, blob_store):
    return PackageService(
        db_session,
        blob_store,
        public_read=True,
        auto_publish=False,
        public_url=PUBLIC_URL,
    )


@pytest.fixture
def private_service(db_session, blob_store):
    return PackageService(
        db_session, blob_store, public_read=False, auto_publish=False, public_url=PUBLIC_URL
    )


@pytest.fixture(params=[True, False], ids=["public-read", "private-read"])
def any_service(request, db_session, blob_store):
    return PackageService(
        db_session, blob_store, public_read=request.param, auto_publish=False, public_url=PUBLIC_URL
    )


@pytest.fixture
def skills(db_session):
    return SkillService(db_session, public_read=True)


@pytest.fixture
def versions(db_session):
    return VersionService(db_session, public_read=True, public_url=PUBLIC_URL)


class TestLimitsAlignment:
    def test_both_sets_of_limit_fields_stay_aligned(self):
        configured = set(LimitsSettings.model_fields)
        package = {field.name for field in dataclasses.fields(PackageLimits)}
        assert configured & package == {
            "max_package_bytes",
            "max_uncompressed_bytes",
            "max_file_bytes",
            "max_files",
        }
        assert configured - package == {"max_inline_read_bytes"}
        assert package - configured == {"max_path_length"}

    def test_every_shared_field_is_copied_explicitly(self):
        configured = LimitsSettings(
            max_package_bytes=11,
            max_uncompressed_bytes=22,
            max_file_bytes=33,
            max_files=44,
            max_inline_read_bytes=55,
        )
        limits = limits_from_settings(configured)
        assert (
            limits.max_package_bytes,
            limits.max_uncompressed_bytes,
            limits.max_file_bytes,
            limits.max_files,
        ) == (11, 22, 33, 44)
        assert limits.max_path_length == DEFAULT_LIMITS.max_path_length

    def test_the_service_defaults_follow_the_settings(self, db_session, blob_store, monkeypatch):
        registry = settings.registry
        monkeypatch.setattr(registry, "public_read", False)
        monkeypatch.setattr(registry, "auto_publish", True)
        monkeypatch.setattr(registry.limits, "max_package_bytes", 1234)
        monkeypatch.setattr(registry.limits, "max_inline_read_bytes", 77)
        monkeypatch.setattr(settings.server, "public_url", "https://configured.example")

        service = PackageService(db_session, blob_store)

        assert service.limits == limits_from_settings(registry.limits)
        assert service.limits.max_package_bytes == 1234
        assert service.public_read is False
        assert service.auto_publish is True
        assert service.max_inline_read_bytes == 77
        assert service.public_url == "https://configured.example"

    def test_explicit_arguments_override_the_settings(self, db_session, blob_store):
        limits = PackageLimits(max_package_bytes=10)
        service = PackageService(
            db_session,
            blob_store,
            limits=limits,
            public_read=False,
            auto_publish=True,
            max_inline_read_bytes=5,
            public_url="https://other.example/",
        )
        assert service.limits is limits
        assert (service.public_read, service.auto_publish) == (False, True)
        assert service.max_inline_read_bytes == 5
        assert service.public_url == "https://other.example"


class TestEncodedPackageLimit:
    @pytest.mark.parametrize(
        ("size", "encoded"),
        [
            (0, 0),
            (1, 4),
            (2, 4),
            (3, 4),
            (4, 8),
            (5, 8),
            (6, 8),
            (7, 12),
            (25 * 1024 * 1024, 34952536),
        ],
    )
    def test_the_length_is_the_one_of_the_standard_base64_alphabet(self, size, encoded):
        assert base64_length(size) == encoded
        assert base64_length(size) == len(base64.b64encode(b"\x00" * size))

    @pytest.mark.parametrize("limit", [1, 2, 3, 4, 999, 1000, 1001, 1002, 1003, 26214400])
    def test_the_longest_valid_encoding_fits_and_one_more_group_does_not(self, limit):
        limits = PackageLimits(max_package_bytes=limit)

        assert encoded_package_limit(limits) == len(base64.b64encode(b"x" * limit))
        assert len(base64.b64encode(b"x" * (limit + 1))) in {
            encoded_package_limit(limits),
            encoded_package_limit(limits) + 4,
        }
        assert base64_length(limit + 3) == encoded_package_limit(limits) + 4

    def test_the_default_follows_the_settings_of_the_moment(self, monkeypatch):
        monkeypatch.setattr(settings.registry.limits, "max_package_bytes", 3000)
        assert encoded_package_limit() == base64_length(3000) == 4000
        monkeypatch.setattr(settings.registry.limits, "max_package_bytes", 3001)
        assert encoded_package_limit() == 4004

    def test_explicit_limits_win_over_the_settings(self, monkeypatch):
        monkeypatch.setattr(settings.registry.limits, "max_package_bytes", 3000)

        assert encoded_package_limit(PackageLimits(max_package_bytes=30)) == base64_length(30)

    def test_the_service_and_the_helper_read_the_same_limit(self, service):
        assert encoded_package_limit(service.limits) == encoded_package_limit()


class TestUpload:
    async def test_a_skill_package_creates_the_entry_and_a_draft_version(
        self, service, actors, blob_store
    ):
        data = make_skill_zip(
            "my-skill",
            "1.0.0",
            description="Does useful things",
            tags=["Python", "cli"],
            category="Dev-Tools",
            extra_frontmatter={"author": "Ada"},
            files={"scripts/run.py": "print(1)\n", "data.bin": b"\x00\x01"},
        )

        result = await service.upload(actors.owner, data)

        assert isinstance(result, UploadResult)
        skill, version = result.skill, result.version
        assert skill.name == "my-skill"
        assert skill.display_name == "My Skill"
        assert skill.description == "Does useful things"
        assert skill.kind == "skill"
        assert skill.category == "dev-tools"
        assert skill.tags == ["cli", "python"]
        assert skill.status == "draft"
        assert skill.latest_version is None
        assert skill.owner_id == actors.owner.user_id

        assert version.skill_id == skill.id
        assert version.version == "1.0.0"
        assert version.status == "draft"
        assert version.sha256 == sha(data)
        assert version.size_bytes == len(data)
        assert version.file_count == 3
        assert version.uncompressed_bytes == sum(
            len(zip_member(data, name)) for name in ("SKILL.md", "scripts/run.py", "data.bin")
        )
        assert version.entrypoint == "SKILL.md"
        assert version.tags == ["python", "cli"]
        assert version.category == "dev-tools"
        assert version.uploaded_by == actors.owner.user_id
        assert version.published_by is None
        assert version.published_at is None
        assert version.deprecated_at is None
        assert await blob_store.get(version.sha256) == data

    async def test_the_extra_frontmatter_is_kept_as_version_metadata(
        self, service, versions, actors
    ):
        data = make_skill_zip(
            "meta-skill", "1.0.0", extra_frontmatter={"author": "Ada", "links": ["a", "b"]}
        )
        await service.upload(actors.owner, data, publish=True)
        detail = await versions.get(None, "meta-skill", "1.0.0")
        assert detail.metadata == {"author": "Ada", "links": ["a", "b"]}

    async def test_a_grounding_package_uses_the_index_entrypoint(self, service, actors):
        data = make_grounding_zip("api-reference", "2.1.0", description="Reference docs")

        result = await service.upload(actors.owner, data)

        assert result.skill.kind == "grounding"
        assert result.version.entrypoint == "INDEX.md"
        assert result.skill.display_name == "Api Reference"

    @pytest.mark.parametrize(
        ("publish", "auto_publish", "expected"),
        [
            (None, False, "draft"),
            (None, True, "published"),
            (True, False, "published"),
            (False, True, "draft"),
            (True, True, "published"),
            (False, False, "draft"),
        ],
    )
    async def test_publication_follows_the_argument_then_the_default(
        self, db_session, blob_store, actors, publish, auto_publish, expected
    ):
        service = PackageService(db_session, blob_store, auto_publish=auto_publish)

        result = await service.upload(
            actors.owner, make_skill_zip("flagged", "1.0.0"), publish=publish
        )

        assert result.version.status == expected
        assert result.skill.status == expected
        assert (result.skill.latest_version == "1.0.0") is (expected == "published")

    async def test_a_published_upload_records_the_publisher_and_is_publicly_visible(
        self, service, skills, actors
    ):
        result = await service.upload(
            actors.owner, make_skill_zip("live-skill", "1.0.0"), publish=True
        )

        assert result.version.status == "published"
        assert result.version.published_by == actors.owner.user_id
        assert result.version.published_at is not None
        assert result.skill.status == "published"
        assert result.skill.latest_version == "1.0.0"
        detail = await skills.get(None, "live-skill")
        assert (detail.status, detail.latest_version) == ("published", "1.0.0")

    async def test_a_draft_upload_is_invisible_to_the_public(self, service, skills, actors):
        await service.upload(actors.owner, make_skill_zip("quiet-skill", "1.0.0"))

        with pytest.raises(NotFoundError):
            await skills.get(None, "quiet-skill")
        with pytest.raises(NotFoundError):
            await skills.get(actors.stranger, "quiet-skill")
        assert (await skills.get(actors.owner, "quiet-skill")).status == "draft"
        assert (await skills.get(actors.admin, "quiet-skill")).status == "draft"

    async def test_a_wrapper_directory_is_removed_and_remembered(
        self, service, session_factory, actors
    ):
        data = make_skill_zip("wrapped", "1.0.0", wrapper="wrapped-1.0.0", files={"docs/a.md": "A"})

        result = await service.upload(actors.owner, data, publish=True)

        stored = await stored_version(session_factory, "wrapped", "1.0.0")
        assert stored.root_prefix == "wrapped-1.0.0/"
        assert result.version.file_count == 2
        listing = await service.manifest(None, "wrapped", "1.0.0")
        assert [entry.path for entry in listing.files] == ["SKILL.md", "docs/a.md"]
        content = await service.read_file(None, "wrapped", "1.0.0", "docs/a.md")
        assert content.content == "A"

    async def test_macos_junk_is_ignored(self, service, actors):
        data = make_skill_zip("junky", "1.0.0", files={"a.txt": "a"}, macos_junk=True)
        await service.upload(actors.owner, data, publish=True)
        listing = await service.manifest(None, "junky", "1.0.0")
        assert [entry.path for entry in listing.files] == ["SKILL.md", "a.txt"]

    async def test_the_version_can_be_given_as_an_argument(self, service, actors):
        data = make_skill_zip("late-version", None)
        result = await service.upload(actors.owner, data, version="1.2.3")
        assert result.version.version == "1.2.3"

    async def test_a_matching_version_argument_is_accepted(self, service, actors):
        data = make_skill_zip("both-versions", "1.2.3")
        result = await service.upload(actors.owner, data, version="1.2.3")
        assert result.version.version == "1.2.3"

    async def test_a_conflicting_version_argument_is_rejected(
        self, service, actors, session_factory, blob_store
    ):
        with pytest.raises(InvalidPackageError) as error:
            await service.upload(actors.owner, make_skill_zip("clash", "1.0.0"), version="2.0.0")
        assert error.value.reason == "VERSION_MISMATCH"
        assert await row_counts(session_factory) == (0, 0)
        assert stored_blobs(blob_store) == []

    @pytest.mark.parametrize("version", ["1.0", "v1.0.0", "1.0.0+build", "", "x" * 60, 3])
    async def test_an_invalid_version_argument_is_a_validation_error(
        self, service, actors, session_factory, version
    ):
        with pytest.raises(InvalidVersionError):
            await service.upload(actors.owner, make_skill_zip("bad-arg", "1.0.0"), version=version)
        assert await row_counts(session_factory) == (0, 0)

    async def test_a_missing_version_is_reported(self, service, actors):
        with pytest.raises(InvalidPackageError) as error:
            await service.upload(actors.owner, make_skill_zip("no-version", None))
        assert error.value.reason == "MISSING_FIELD"

    async def test_a_second_version_joins_the_existing_entry(
        self, service, skills, actors, session_factory
    ):
        first = await service.upload(actors.owner, make_skill_zip("growing", "1.0.0"), publish=True)
        second = await service.upload(
            actors.owner, make_skill_zip("growing", "1.1.0", description="newer")
        )

        assert second.skill.id == first.skill.id
        assert second.version.skill_id == first.skill.id
        assert second.skill.latest_version == "1.0.0"
        assert await row_counts(session_factory) == (1, 2)
        detail = await skills.get(actors.owner, "growing")
        assert [item.version for item in detail.versions] == ["1.1.0", "1.0.0"]

    async def test_an_admin_can_add_a_version_to_an_entry_of_another_user(self, service, actors):
        await service.upload(actors.owner, make_skill_zip("shared-skill", "1.0.0"))

        result = await service.upload(
            actors.admin, make_skill_zip("shared-skill", "1.1.0"), publish=True
        )

        assert result.version.uploaded_by == actors.admin.user_id
        assert result.version.published_by == actors.admin.user_id
        assert result.skill.owner_id == actors.owner.user_id

    async def test_an_admin_uploading_a_new_entry_becomes_its_owner(self, service, actors):
        result = await service.upload(actors.admin, make_skill_zip("admin-made", "1.0.0"))
        assert result.skill.owner_id == actors.admin.user_id

    @pytest.mark.parametrize("name", ["stranger", "stranger_read", "owner_read", "admin_read"])
    async def test_other_writers_and_readers_cannot_add_versions(
        self, any_service, actors, blob_store, session_factory, name
    ):
        await any_service.upload(actors.owner, make_skill_zip("guarded", "1.0.0"), publish=True)
        before_blobs = stored_blobs(blob_store)

        with pytest.raises(ForbiddenError):
            await any_service.upload(actors.get(name), make_skill_zip("guarded", "1.1.0"))

        assert await row_counts(session_factory) == (1, 1)
        assert stored_blobs(blob_store) == before_blobs

    async def test_anonymous_uploads_are_unauthorized(
        self, any_service, session_factory, blob_store
    ):
        with pytest.raises(UnauthorizedError):
            await any_service.upload(None, make_skill_zip("anon", "1.0.0"))
        assert await row_counts(session_factory) == (0, 0)
        assert stored_blobs(blob_store) == []

    @pytest.mark.parametrize(
        ("name", "error"),
        [
            ("anonymous", UnauthorizedError),
            ("stranger_read", ForbiddenError),
            ("owner_read", ForbiddenError),
            ("admin_read", ForbiddenError),
        ],
    )
    async def test_authorization_is_checked_before_the_package_is_even_looked_at(
        self, any_service, actors, name, error
    ):
        with pytest.raises(error):
            await any_service.upload(actors.get(name), b"definitely not a zip")

    async def test_a_read_scope_cannot_create_new_entries_either(
        self, service, actors, session_factory
    ):
        with pytest.raises(ForbiddenError):
            await service.upload(actors.owner_read, make_skill_zip("fresh", "1.0.0"))
        assert await row_counts(session_factory) == (0, 0)

    async def test_uploading_to_a_private_entry_of_another_user_is_forbidden_not_hidden(
        self, service, skills, actors
    ):
        await service.upload(actors.owner, make_skill_zip("private-draft", "1.0.0"))
        with pytest.raises(ForbiddenError):
            await service.upload(actors.stranger, make_skill_zip("private-draft", "1.1.0"))

    async def test_changing_the_kind_of_an_entry_is_a_conflict(
        self, service, actors, session_factory
    ):
        await service.upload(actors.owner, make_skill_zip("same-name", "1.0.0"))

        with pytest.raises(ConflictError, match="skill"):
            await service.upload(actors.owner, make_grounding_zip("same-name", "1.1.0"))
        with pytest.raises(ConflictError):
            await service.upload(actors.admin, make_grounding_zip("same-name", "1.2.0"))

        assert await row_counts(session_factory) == (1, 1)

    async def test_a_stranger_learns_nothing_about_the_kind_of_a_foreign_entry(
        self, service, actors
    ):
        await service.upload(actors.owner, make_skill_zip("kind-secret", "1.0.0"))
        with pytest.raises(ForbiddenError) as error:
            await service.upload(actors.stranger, make_grounding_zip("kind-secret", "1.0.0"))
        assert "skill" not in error.value.message
        assert "grounding" not in error.value.message

    async def test_a_repeated_version_is_a_conflict_even_with_other_content(
        self, service, actors, session_factory, blob_store
    ):
        original = make_skill_zip("repeat", "1.0.0", description="original")
        await service.upload(actors.owner, original, publish=True)
        before = stored_blobs(blob_store)

        with pytest.raises(ConflictError, match="already exists"):
            await service.upload(actors.owner, original)
        with pytest.raises(ConflictError, match="already exists"):
            await service.upload(
                actors.owner, make_skill_zip("repeat", "1.0.0", description="other")
            )

        assert await row_counts(session_factory) == (1, 1)
        assert stored_blobs(blob_store) == before
        stored = await stored_version(session_factory, "repeat", "1.0.0")
        assert stored.blob_sha256 == sha(original)

    async def test_the_version_check_is_case_exact_and_per_entry(
        self, service, skills, versions, actors, session_factory
    ):
        await service.upload(actors.owner, make_skill_zip("entry-a", "1.0.0"))
        await service.upload(actors.owner, make_skill_zip("entry-b", "1.0.0"))
        await service.upload(actors.owner, make_skill_zip("entry-a", "1.0.0-rc.1"))
        await service.upload(actors.owner, make_skill_zip("entry-a", "1.0.0-RC.1"))

        assert await row_counts(session_factory) == (2, 4)
        listed = await versions.list(actors.owner, "entry-a")
        assert [item.version for item in listed] == ["1.0.0", "1.0.0-rc.1", "1.0.0-RC.1"]
        with pytest.raises(ConflictError):
            await service.upload(actors.owner, make_skill_zip("entry-a", "1.0.0-RC.1"))

    async def test_a_package_over_the_limit_is_rejected_with_payload_too_large(
        self, db_session, blob_store, actors, session_factory
    ):
        data = make_skill_zip("big-one", "1.0.0", files={"payload.bin": RANDOM_BLOB[:2000]})
        small = PackageService(
            db_session, blob_store, limits=PackageLimits(max_package_bytes=len(data) - 1)
        )

        with pytest.raises(PayloadTooLargeError):
            await small.upload(actors.owner, data)

        assert await row_counts(session_factory) == (0, 0)
        assert stored_blobs(blob_store) == []

    async def test_a_package_exactly_at_the_limit_is_accepted(self, db_session, blob_store, actors):
        data = make_skill_zip("edge-one", "1.0.0")
        exact = PackageService(
            db_session, blob_store, limits=PackageLimits(max_package_bytes=len(data))
        )
        result = await exact.upload(actors.owner, data)
        assert result.version.size_bytes == len(data)

    async def test_the_default_limit_comes_from_the_settings(
        self, db_session, blob_store, actors, monkeypatch
    ):
        monkeypatch.setattr(settings.registry.limits, "max_package_bytes", 100)
        with pytest.raises(PayloadTooLargeError):
            await PackageService(db_session, blob_store).upload(
                actors.owner, make_skill_zip("settings-limit", "1.0.0")
            )

    @pytest.mark.parametrize(
        ("label", "build", "reason"),
        [
            ("not a zip", lambda: b"this is plain text", "NOT_A_ZIP"),
            ("empty file", lambda: b"", "NOT_A_ZIP"),
            ("no entrypoint", lambda: make_zip({"README.md": "# readme"}), "MISSING_ENTRYPOINT"),
            (
                "both entrypoints",
                lambda: make_zip({"SKILL.md": skill_md("both"), "INDEX.md": index_md("both")}),
                "AMBIGUOUS_ENTRYPOINT",
            ),
            (
                "no frontmatter",
                lambda: make_zip({"SKILL.md": "# just markdown\n"}),
                "BAD_FRONTMATTER",
            ),
            ("invalid name", lambda: make_skill_zip("Not Valid", "1.0.0"), "INVALID_FIELD"),
            (
                "uuid-like name",
                lambda: make_skill_zip("123e4567-e89b-12d3-a456-426614174000", "1.0.0"),
                "INVALID_FIELD",
            ),
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
        ],
    )
    async def test_invalid_packages_are_rejected_with_their_reason_and_leave_no_trace(
        self, service, actors, session_factory, blob_store, label, build, reason
    ):
        with pytest.raises(InvalidPackageError) as error:
            await service.upload(actors.owner, build())

        assert error.value.reason == reason
        assert error.value.code == "INVALID_PACKAGE"
        assert await row_counts(session_factory) == (0, 0)
        assert stored_blobs(blob_store) == []

    @pytest.mark.parametrize("declared_size", [None, 10])
    async def test_a_decompression_bomb_is_rejected_even_with_a_forged_size(
        self, db_session, blob_store, actors, session_factory, declared_size
    ):
        limits = PackageLimits(max_uncompressed_bytes=2 * 1024 * 1024, max_file_bytes=1024 * 1024)
        strict = PackageService(db_session, blob_store, limits=limits)
        bomb = make_bomb_zip(8 * 1024 * 1024, declared_size=declared_size)
        assert len(bomb) < 64 * 1024

        with pytest.raises(InvalidPackageError) as error:
            await strict.upload(actors.owner, bomb)

        assert error.value.reason == "TOO_LARGE"
        assert await row_counts(session_factory) == (0, 0)
        assert stored_blobs(blob_store) == []

    async def test_limits_inside_the_package_are_enforced_by_the_inspector(
        self, db_session, blob_store, actors
    ):
        service = PackageService(db_session, blob_store, limits=PackageLimits(max_files=2))
        data = make_skill_zip("many-files", "1.0.0", files={"a": "1", "b": "2", "c": "3"})
        with pytest.raises(InvalidPackageError) as error:
            await service.upload(actors.owner, data)
        assert error.value.reason == "TOO_MANY_FILES"

    @pytest.mark.parametrize("payload", ["text", None, 12, ["PK"]])
    async def test_non_bytes_content_is_a_validation_error(self, service, actors, payload):
        with pytest.raises(ValidationError):
            await service.upload(actors.owner, payload)

    @pytest.mark.parametrize("publish", ["false", "true", 1, 0, "", [], {}])
    async def test_publish_must_be_a_real_boolean(self, service, actors, session_factory, publish):
        with pytest.raises(ValidationError, match="boolean"):
            await service.upload(actors.owner, make_skill_zip("strict", "1.0.0"), publish=publish)
        assert await row_counts(session_factory) == (0, 0)

    async def test_a_huge_integer_is_not_expanded_into_a_zero_filled_buffer(self, service, actors):
        with pytest.raises(ValidationError):
            await service.upload(actors.owner, 10**12)

    @pytest.mark.parametrize("wrap", [bytearray, memoryview])
    async def test_buffer_types_are_accepted(self, service, actors, wrap):
        data = make_skill_zip("buffered", "1.0.0")
        result = await service.upload(actors.owner, wrap(data))
        assert result.version.sha256 == sha(data)

    async def test_uploading_works_on_a_private_registry(self, private_service, actors):
        result = await private_service.upload(actors.owner, make_skill_zip("private-up", "1.0.0"))
        assert result.version.version == "1.0.0"

    async def test_uploading_to_a_hidden_entry_keeps_it_hidden(self, service, skills, actors):
        await service.upload(actors.owner, make_skill_zip("hush", "1.0.0"), publish=True)
        await skills.hide(actors.owner, "hush")

        result = await service.upload(actors.owner, make_skill_zip("hush", "1.1.0"), publish=True)

        assert result.skill.status == "unpublished"
        assert result.skill.latest_version == "1.1.0"
        with pytest.raises(NotFoundError):
            await skills.get(None, "hush")

    async def test_uploads_are_logged_and_rejections_warn(self, service, actors, caplog):
        with caplog.at_level(logging.INFO, logger="app.services.package"):
            await service.upload(actors.owner, make_skill_zip("logged", "1.0.0"), publish=True)
            with pytest.raises(ConflictError):
                await service.upload(actors.owner, make_skill_zip("logged", "1.0.0"))
            with pytest.raises(InvalidPackageError):
                await service.upload(actors.owner, b"junk")
            with pytest.raises(ForbiddenError):
                await service.upload(actors.stranger, make_skill_zip("logged", "2.0.0"))

        info = [r for r in caplog.records if r.levelno == logging.INFO]
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert [r.getMessage() for r in info] == ["Package uploaded: logged 1.0.0"]
        assert info[0].extra_data["sha256"] == sha(make_skill_zip("logged", "1.0.0"))
        assert info[0].extra_data["user_id"] == actors.owner.user_id
        assert "Upload rejected: duplicate version" in warnings
        assert "Upload rejected: invalid package" in warnings
        assert "Upload rejected: entry belongs to another user" in warnings
        invalid = next(r for r in caplog.records if r.getMessage().endswith("invalid package"))
        assert invalid.extra_data["reason"] == "NOT_A_ZIP"

    async def test_oversized_uploads_are_logged(self, db_session, blob_store, actors, caplog):
        service = PackageService(db_session, blob_store, limits=PackageLimits(max_package_bytes=10))
        with (
            caplog.at_level(logging.WARNING, logger="app.services.package"),
            pytest.raises(PayloadTooLargeError),
        ):
            await service.upload(actors.owner, make_skill_zip("huge", "1.0.0"))
        assert any(r.getMessage() == "Upload rejected: payload too large" for r in caplog.records)


@pytest_asyncio.fixture
async def file_factory(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'race.db'}", connect_args={"timeout": 60}
    )
    configure_sqlite_engine(engine)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


class TestUploadFailures:
    async def test_a_persistent_conflict_is_retried_once_then_reported_and_rolled_back(
        self, service, actors, session_factory, monkeypatch, caplog
    ):
        await service.upload(actors.owner, make_skill_zip("racy", "1.0.0"), publish=True)
        calls = []

        async def blind(db, skill_id, version):
            calls.append(version)

        monkeypatch.setattr(skill_repo, "get_version", blind)
        with (
            caplog.at_level(logging.WARNING, logger="app.services.package"),
            pytest.raises(ConflictError, match="concurrent"),
        ):
            await service.upload(actors.owner, make_skill_zip("racy", "1.0.0", description="b"))
        monkeypatch.undo()

        assert calls == ["1.0.0", "1.0.0"]
        rejections = [r for r in caplog.records if r.getMessage().endswith("concurrent change")]
        assert len(rejections) == 2
        assert await row_counts(session_factory) == (1, 1)
        later = await service.upload(actors.owner, make_skill_zip("racy", "1.1.0"))
        assert later.version.version == "1.1.0"
        assert await row_counts(session_factory) == (1, 2)

    async def test_a_lost_race_on_the_version_ends_as_a_precise_conflict_after_the_retry(
        self, service, actors, session_factory, monkeypatch
    ):
        await service.upload(actors.owner, make_skill_zip("racy-twice", "1.0.0"))
        original = skill_repo.get_version
        calls = []

        async def blind_once(db, skill_id, version):
            calls.append(version)
            if len(calls) == 1:
                return None
            return await original(db, skill_id, version)

        monkeypatch.setattr(skill_repo, "get_version", blind_once)
        with pytest.raises(ConflictError, match="already exists"):
            await service.upload(
                actors.owner, make_skill_zip("racy-twice", "1.0.0", description="b")
            )
        monkeypatch.undo()

        assert len(calls) == 2
        assert await row_counts(session_factory) == (1, 1)

    async def test_a_lost_race_on_the_entry_name_is_retried_against_the_new_entry(
        self, service, actors, session_factory, monkeypatch
    ):
        await service.upload(actors.owner, make_skill_zip("racy-name", "1.0.0"))
        original = skill_repo.get_skill_by_name
        calls = []

        async def blind_once(db, name):
            calls.append(name)
            if len(calls) == 1:
                return None
            return await original(db, name)

        monkeypatch.setattr(skill_repo, "get_skill_by_name", blind_once)
        result = await service.upload(actors.owner, make_skill_zip("racy-name", "2.0.0"))
        monkeypatch.undo()

        assert len(calls) == 2
        assert result.version.version == "2.0.0"
        assert await row_counts(session_factory) == (1, 2)

    async def test_a_lost_race_on_the_entry_name_against_another_user_ends_as_forbidden(
        self, service, actors, session_factory, monkeypatch
    ):
        await service.upload(actors.owner, make_skill_zip("racy-owner", "1.0.0"))
        original = skill_repo.get_skill_by_name
        calls = []

        async def blind_once(db, name):
            calls.append(name)
            if len(calls) == 1:
                return None
            return await original(db, name)

        monkeypatch.setattr(skill_repo, "get_skill_by_name", blind_once)
        with pytest.raises(ForbiddenError):
            await service.upload(actors.stranger, make_skill_zip("racy-owner", "2.0.0"))
        monkeypatch.undo()

        assert await row_counts(session_factory) == (1, 1)

    async def test_a_lost_race_on_the_entry_name_that_never_resolves_is_a_conflict(
        self, service, actors, session_factory, monkeypatch
    ):
        await service.upload(actors.owner, make_skill_zip("racy-blind", "1.0.0"))

        async def blind(db, name):
            return None

        monkeypatch.setattr(skill_repo, "get_skill_by_name", blind)
        with pytest.raises(ConflictError):
            await service.upload(actors.owner, make_skill_zip("racy-blind", "2.0.0"))
        monkeypatch.undo()

        assert await row_counts(session_factory) == (1, 1)
        assert (await service.upload(actors.owner, make_skill_zip("racy-blind", "2.0.0"))).version

    async def test_a_failure_after_the_entry_was_created_leaves_no_entry_behind(
        self, service, actors, session_factory, monkeypatch
    ):
        async def broken(*args, **kwargs):
            raise RuntimeError("database went away")

        monkeypatch.setattr(skill_repo, "create_version", broken)
        with pytest.raises(RuntimeError, match="database went away"):
            await service.upload(actors.owner, make_skill_zip("half-done", "1.0.0"))
        monkeypatch.undo()

        assert await row_counts(session_factory) == (0, 0)
        result = await service.upload(actors.owner, make_skill_zip("half-done", "1.0.0"))
        assert result.skill.name == "half-done"
        assert await row_counts(session_factory) == (1, 1)

    async def test_a_failure_while_recalculating_leaves_the_catalog_untouched(
        self, service, actors, session_factory, monkeypatch
    ):
        await service.upload(actors.owner, make_skill_zip("keep-state", "1.0.0"), publish=True)

        async def broken(*args, **kwargs):
            raise RuntimeError("recalculation failed")

        monkeypatch.setattr(package_service_module.catalog, "refresh_skill", broken)
        with pytest.raises(RuntimeError):
            await service.upload(actors.owner, make_skill_zip("keep-state", "1.1.0"), publish=True)
        monkeypatch.undo()

        assert await row_counts(session_factory) == (1, 1)
        async with session_factory() as session:
            skill = await skill_repo.get_skill_by_name(session, "keep-state")
            assert (skill.status, skill.latest_version) == (SkillStatus.PUBLISHED, "1.0.0")

    async def test_a_blob_store_failure_is_reported_as_a_storage_error_without_rows(
        self, service, actors, session_factory, monkeypatch, blob_store, caplog
    ):
        async def failing_put(key, data):
            raise BlobStoreError("disk full")

        monkeypatch.setattr(blob_store, "put", failing_put)
        with (
            caplog.at_level(logging.ERROR, logger="app.services.package"),
            pytest.raises(StorageError),
        ):
            await service.upload(actors.owner, make_skill_zip("no-space", "1.0.0"))

        assert await row_counts(session_factory) == (0, 0)
        assert any("could not be stored" in r.getMessage() for r in caplog.records)

    async def test_two_real_concurrent_uploads_of_the_same_version_yield_one_winner(
        self, file_factory, blob_store
    ):
        async with file_factory() as setup:
            owner = (await make_actors(setup)).owner
        data = make_skill_zip("raced", "1.0.0")

        async def attempt():
            async with file_factory() as session:
                try:
                    return await PackageService(session, blob_store).upload(owner, data)
                except ConflictError as error:
                    return error

        outcomes = await asyncio.gather(attempt(), attempt())

        winners = [item for item in outcomes if isinstance(item, UploadResult)]
        losers = [item for item in outcomes if isinstance(item, ConflictError)]
        assert (len(winners), len(losers)) == (1, 1)
        assert await row_counts(file_factory) == (1, 1)

    async def test_concurrent_uploads_of_different_versions_all_succeed_and_keep_the_catalog_consistent(
        self, file_factory, blob_store
    ):
        async with file_factory() as setup:
            owner = (await make_actors(setup)).owner

        async def attempt(version):
            async with file_factory() as session:
                try:
                    return await PackageService(session, blob_store).upload(
                        owner, make_skill_zip("raced-two", version), publish=True
                    )
                except ConflictError as error:
                    return error

        outcomes = await asyncio.gather(*(attempt(f"1.0.{n}") for n in range(4)))

        assert all(isinstance(item, UploadResult) for item in outcomes), outcomes
        assert await row_counts(file_factory) == (1, 4)
        async with file_factory() as session:
            skill = await skill_repo.get_skill_by_name(session, "raced-two")
            assert skill.status == SkillStatus.PUBLISHED
            assert skill.latest_version == "1.0.3"


@pytest_asyncio.fixture
async def docs(service, actors):
    result = await upload_package(
        service, actors.owner, "docs-skill", "1.0.0", files=FILES, publish=True
    )
    return result


@pytest_asyncio.fixture
async def grounded(service, actors):
    return await upload_package(
        service,
        actors.owner,
        "ref-docs",
        "1.0.0",
        kind="grounding",
        files={"tables/a.csv": "a,b\n1,2\n"},
        publish=True,
    )


class TestContractShapes:
    async def test_every_response_has_exactly_the_contract_keys(self, service, actors, docs):
        upload = docs.model_dump(mode="json")
        listing = (await service.manifest(None, "docs-skill")).model_dump(mode="json")
        entrypoint = (await service.entrypoint(None, "docs-skill")).model_dump(mode="json")
        content = (await service.read_file(None, "docs-skill", "latest", "empty.txt")).model_dump(
            mode="json"
        )
        info = (await service.download_info(None, "docs-skill")).model_dump(mode="json")

        file_keys = {
            "path",
            "size",
            "sha256",
            "media_type",
            "text",
            "content",
            "truncated",
            "encoding",
        }
        assert set(upload) == {"skill", "version"}
        assert set(listing) == {"name", "version", "files"}
        assert set(listing["files"][0]) == {"path", "size", "sha256", "media_type", "text"}
        assert set(content) == file_keys
        assert set(entrypoint) == file_keys | {"name", "version", "kind"}
        assert set(info) == {"name", "version", "sha256", "size_bytes", "url", "requires_auth"}

    async def test_the_version_detail_adds_metadata_and_the_download_url(
        self, versions, actors, docs
    ):
        detail = (await versions.get(None, "docs-skill", "latest")).model_dump(mode="json")
        summary = (await versions.list(None, "docs-skill"))[0].model_dump(mode="json")
        assert set(detail) == set(summary) | {"metadata", "download_url"}


class TestManifest:
    async def test_lists_every_file_sorted_with_hash_size_and_type(self, service, actors, docs):
        listing = await service.manifest(None, "docs-skill", "1.0.0")

        assert isinstance(listing, FileListing)
        assert listing.name == "docs-skill"
        assert listing.version == "1.0.0"
        paths = [entry.path for entry in listing.files]
        assert paths == sorted(paths)
        assert set(paths) == {"SKILL.md", *FILES}
        by_path = {entry.path: entry for entry in listing.files}
        for path, content in FILES.items():
            raw = content.encode("utf-8") if isinstance(content, str) else content
            assert by_path[path].size == len(raw)
            assert by_path[path].sha256 == sha(raw)
        assert by_path["docs/guide.md"].media_type == "text/markdown"
        assert by_path["scripts/run.py"].media_type == "text/x-python"
        assert by_path["notes/ünïcode.txt"].media_type == "text/plain"
        assert by_path["data/table.bin"].media_type == "application/octet-stream"
        assert by_path["data/table.bin"].text is False
        assert by_path["docs/guide.md"].text is True
        assert by_path["empty.txt"].text is True

    async def test_latest_is_resolved_to_the_concrete_version(self, service, actors, docs):
        listing = await service.manifest(None, "docs-skill", "latest")
        assert listing.version == "1.0.0"
        assert (await service.manifest(None, "docs-skill")).version == "1.0.0"

    async def test_lookup_by_id(self, service, actors, docs):
        listing = await service.manifest(None, docs.skill.id, "1.0.0")
        assert listing.name == "docs-skill"

    async def test_the_listing_is_json_ready(self, service, actors, docs):
        dumped = (await service.manifest(None, "docs-skill")).model_dump(mode="json")
        assert set(dumped) == {"name", "version", "files"}
        assert set(dumped["files"][0]) == {"path", "size", "sha256", "media_type", "text"}


class TestEntrypoint:
    async def test_returns_the_skill_document_with_the_resolved_identity(
        self, service, actors, docs, skills
    ):
        content = await service.entrypoint(None, "docs-skill", "latest")

        assert isinstance(content, EntrypointContent)
        assert (content.name, content.version, content.kind) == ("docs-skill", "1.0.0", "skill")
        assert content.path == "SKILL.md"
        assert content.text is True
        assert content.truncated is False
        assert content.encoding == "utf-8"
        original = make_skill_zip("docs-skill", "1.0.0", files=FILES)
        expected = zip_member(original, "SKILL.md").decode("utf-8")
        assert content.content == expected
        assert content.sha256 == sha(expected)
        assert content.size == len(expected.encode("utf-8"))
        assert content.media_type == "text/markdown"

    async def test_grounding_entries_serve_their_index(self, service, actors, grounded):
        content = await service.entrypoint(None, "ref-docs", "1.0.0")
        assert (content.path, content.kind) == ("INDEX.md", "grounding")
        assert content.content.startswith("---\nname: ref-docs")

    async def test_a_wrapped_package_serves_its_entrypoint_without_the_wrapper(
        self, service, actors
    ):
        await service.upload(
            actors.owner, make_skill_zip("wrapped-ep", "1.0.0", wrapper="top"), publish=True
        )
        content = await service.entrypoint(None, "wrapped-ep", "latest")
        assert content.path == "SKILL.md"
        assert "name: wrapped-ep" in content.content

    async def test_a_long_entrypoint_is_truncated_on_a_character_boundary(
        self, db_session, blob_store, actors
    ):
        header = len(skill_md("long-doc", "1.0.0", body="").encode("utf-8"))
        service = PackageService(db_session, blob_store, max_inline_read_bytes=header + 17)
        await service.upload(
            actors.owner, make_skill_zip("long-doc", "1.0.0", body="é" * 500), publish=True
        )

        content = await service.entrypoint(None, "long-doc", "latest")

        assert content.truncated is True
        assert content.size == header + 1000
        assert content.content.endswith("é" * 8)
        assert len(content.content.encode("utf-8")) == header + 16
        assert "\ufffd" not in content.content


class TestReadFile:
    async def test_reads_a_text_file(self, service, actors, docs):
        content = await service.read_file(None, "docs-skill", "1.0.0", "docs/guide.md")

        assert isinstance(content, FileContent)
        assert content.path == "docs/guide.md"
        assert content.content == FILES["docs/guide.md"]
        assert content.text is True
        assert content.truncated is False
        assert content.encoding == "utf-8"
        assert content.media_type == "text/markdown"
        assert content.size == len(FILES["docs/guide.md"].encode("utf-8"))
        assert content.sha256 == sha(FILES["docs/guide.md"])

    async def test_reads_a_file_whose_path_has_non_ascii_characters(self, service, actors, docs):
        content = await service.read_file(None, "docs-skill", "latest", "notes/ünïcode.txt")
        assert content.content == "héllo wörld\n"

    async def test_reads_an_empty_file(self, service, actors, docs):
        content = await service.read_file(None, "docs-skill", "1.0.0", "empty.txt")
        assert (content.content, content.size, content.truncated) == ("", 0, False)

    async def test_binary_files_are_described_without_content(self, service, actors, docs):
        content = await service.read_file(None, "docs-skill", "1.0.0", "data/table.bin")

        assert content.text is False
        assert content.content is None
        assert content.truncated is False
        assert content.size == 256
        assert content.sha256 == sha(bytes(range(256)))
        assert content.media_type == "application/octet-stream"

    async def test_reads_by_latest_and_by_id(self, service, actors, docs):
        by_latest = await service.read_file(None, "docs-skill", "latest", "scripts/run.py")
        by_id = await service.read_file(None, docs.skill.id, "1.0.0", "scripts/run.py")
        assert by_latest == by_id
        assert by_latest.content == "print('hi')\n"

    async def test_large_text_files_are_truncated_to_the_inline_limit(
        self, db_session, blob_store, actors, docs
    ):
        small = PackageService(db_session, blob_store, max_inline_read_bytes=1000)

        content = await small.read_file(None, "docs-skill", "1.0.0", "big.txt")

        assert content.truncated is True
        assert content.content == ("line\n" * 400)[:1000]
        assert len(content.content) == 1000
        assert content.size == 2000
        assert content.sha256 == sha("line\n" * 400)
        assert content.text is True

    async def test_a_file_exactly_at_the_inline_limit_is_not_truncated(
        self, db_session, blob_store, actors, docs
    ):
        exact = PackageService(db_session, blob_store, max_inline_read_bytes=2000)
        content = await exact.read_file(None, "docs-skill", "1.0.0", "big.txt")
        assert content.truncated is False
        assert content.content == "line\n" * 400

    async def test_truncation_never_splits_a_multibyte_character(
        self, db_session, blob_store, actors, docs
    ):
        cut = PackageService(db_session, blob_store, max_inline_read_bytes=101)

        content = await cut.read_file(None, "docs-skill", "1.0.0", "wide.txt")

        assert content.truncated is True
        assert content.content == "é" * 50
        assert "\ufffd" not in content.content
        assert content.size == 400

    async def test_a_cut_on_a_character_boundary_keeps_every_character(
        self, db_session, blob_store, actors, docs
    ):
        cut = PackageService(db_session, blob_store, max_inline_read_bytes=100)
        content = await cut.read_file(None, "docs-skill", "1.0.0", "wide.txt")
        assert content.content == "é" * 50
        assert content.truncated is True

    async def test_truncation_always_yields_the_longest_whole_character_prefix(
        self, db_session, blob_store, service, actors
    ):
        generator = random.Random(7)
        alphabet = ["a", "Z", " ", "\n", "\u00e9", "\u20ac", "\U0001f600"]
        texts = {
            f"t{index}.txt": "".join(generator.choice(alphabet) for _ in range(index % 37))
            for index in range(40)
        }
        await upload_package(service, actors.owner, "prefixes", "1.0.0", files=texts)
        checked = 0

        for path, text in texts.items():
            encoded = text.encode("utf-8")
            limits = {0, 1, 2, 3, 4, 5, 8, 13, 21, len(encoded) - 1, len(encoded), len(encoded) + 1}
            for limit in sorted(item for item in limits if item >= 0):
                reader = PackageService(db_session, blob_store, max_inline_read_bytes=limit)

                content = await reader.read_file(None, "prefixes", "1.0.0", path)

                truncated = len(encoded) > limit
                expected = encoded[:limit].decode("utf-8", errors="ignore") if truncated else text
                assert (content.content, content.truncated) == (expected, truncated), (path, limit)
                assert content.size == len(encoded)
                assert "\ufffd" not in content.content
                checked += 1

        assert checked > 300

    async def test_the_default_inline_limit_comes_from_the_settings(
        self, db_session, blob_store, actors, docs, monkeypatch
    ):
        monkeypatch.setattr(settings.registry.limits, "max_inline_read_bytes", 10)
        content = await PackageService(db_session, blob_store).read_file(
            None, "docs-skill", "1.0.0", "big.txt"
        )
        assert content.truncated is True
        assert content.content == "line\nline\n"

    async def test_invalid_utf8_after_the_sampled_prefix_is_replaced_not_fatal(
        self, service, actors
    ):
        body = b"a" * 9000 + b"\xff\xfe tail"
        await upload_package(
            service, actors.owner, "late-binary", "1.0.0", files={"mixed.txt": body}
        )
        listing = await service.manifest(actors.owner, "late-binary", "1.0.0")
        assert next(e for e in listing.files if e.path == "mixed.txt").text is True

        content = await service.read_file(actors.owner, "late-binary", "1.0.0", "mixed.txt")

        assert content.content.startswith("a" * 9000)
        assert "\ufffd" in content.content
        assert content.content.endswith(" tail")

    async def test_a_wrapped_package_reads_files_by_their_unwrapped_path(self, service, actors):
        await service.upload(
            actors.owner,
            make_skill_zip("wrapped-read", "1.0.0", wrapper="pkg-1", files={"docs/a.md": "A!"}),
            publish=True,
        )
        content = await service.read_file(None, "wrapped-read", "1.0.0", "docs/a.md")
        assert content.content == "A!"
        with pytest.raises(NotFoundError):
            await service.read_file(None, "wrapped-read", "1.0.0", "pkg-1/docs/a.md")

    @pytest.mark.parametrize(
        "path",
        [
            "../etc/passwd",
            "docs/../../etc/passwd",
            "docs/../docs/guide.md",
            "/etc/passwd",
            "/docs/guide.md",
            "docs\\guide.md",
            "docs/guide.md\x00",
            "docs/guide.md\x00.png",
            "",
            ".",
            "..",
            "docs//guide.md",
            "docs/./guide.md",
            "docs/",
            "C:/Windows/win.ini",
            "a" * 300,
            "docs/guide.md\n",
        ],
    )
    async def test_hostile_paths_are_rejected_as_invalid_paths(self, service, actors, docs, path):
        for operation in (
            lambda: service.read_file(None, "docs-skill", "1.0.0", path),
            lambda: service.open_raw(None, "docs-skill", "1.0.0", path),
        ):
            with pytest.raises(ValidationError) as error:
                await operation()
            assert error.value.reason == "INVALID_PATH"

    async def test_a_non_string_path_is_an_invalid_path(self, service, actors, docs):
        with pytest.raises(ValidationError) as error:
            await service.read_file(None, "docs-skill", "1.0.0", None)
        assert error.value.reason == "INVALID_PATH"

    @pytest.mark.parametrize(
        "path",
        ["missing.txt", "docs", "docs/guide.MD", "DOCS/guide.md", "docs/guide.md ", "SKILL.md.bak"],
    )
    async def test_paths_outside_the_manifest_are_not_found(self, service, actors, docs, path):
        with pytest.raises(NotFoundError):
            await service.read_file(None, "docs-skill", "1.0.0", path)
        with pytest.raises(NotFoundError):
            await service.open_raw(None, "docs-skill", "1.0.0", path)

    async def test_path_validation_does_not_leak_through_invisible_entries(
        self, service, actors, docs
    ):
        with pytest.raises(NotFoundError):
            await service.read_file(None, "no-such-entry", "1.0.0", "../x")


class TestRawFile:
    async def test_streams_the_exact_bytes_of_a_text_file(self, service, actors, docs):
        raw = await service.open_raw(None, "docs-skill", "1.0.0", "docs/guide.md")
        content = FILES["docs/guide.md"].encode("utf-8")

        assert isinstance(raw, RawFile)
        assert raw.path == "docs/guide.md"
        assert raw.media_type == "text/markdown"
        assert raw.text is True
        assert raw.size == len(content)
        assert b"".join(await collect(raw.stream)) == content

    async def test_streams_the_exact_bytes_of_a_binary_file(self, service, actors, docs):
        raw = await service.open_raw(None, "docs-skill", "latest", "data/table.bin")
        assert b"".join(await collect(raw.stream)) == bytes(range(256))
        assert raw.size == 256
        assert raw.text is False
        assert raw.media_type == "application/octet-stream"

    async def test_an_empty_file_streams_no_chunks(self, service, actors, docs):
        raw = await service.open_raw(None, "docs-skill", "1.0.0", "empty.txt")

        assert raw.size == 0
        assert await collect(raw.stream) == []

    async def test_is_never_truncated_by_the_inline_limit(
        self, db_session, blob_store, actors, docs
    ):
        tiny = PackageService(db_session, blob_store, max_inline_read_bytes=10)
        raw = await tiny.open_raw(None, "docs-skill", "1.0.0", "big.txt")
        assert b"".join(await collect(raw.stream)) == ("line\n" * 400).encode("utf-8")

    async def test_html_and_scripts_are_reported_with_their_manifest_type_only(
        self, service, actors
    ):
        await upload_package(
            service,
            actors.owner,
            "web-bits",
            "1.0.0",
            files={"page.html": "<script>alert(1)</script>", "icon.svg": "<svg onload=alert(1)/>"},
        )
        page = await service.open_raw(actors.owner, "web-bits", "1.0.0", "page.html")
        assert b"".join(await collect(page.stream)) == b"<script>alert(1)</script>"
        assert page.media_type == "text/html"
        assert page.text is True


class TestStoredDataIntegrity:
    async def tamper(self, db_session, name, version, **fields):
        skill = await skill_repo.get_skill_by_name(db_session, name)
        target = await skill_repo.get_version(db_session, skill.id, version)
        for key, value in fields.items():
            setattr(target, key, value)
        await db_session.commit()

    async def current_manifest(self, service):
        listing = await service.manifest(None, "docs-skill", "1.0.0")
        return [entry.model_dump() for entry in listing.files]

    async def test_a_wrong_hash_in_the_manifest_is_a_storage_error(
        self, service, db_session, docs, caplog
    ):
        manifest = await self.current_manifest(service)
        for entry in manifest:
            if entry["path"] == "docs/guide.md":
                entry["sha256"] = "0" * 64
        await self.tamper(db_session, "docs-skill", "1.0.0", manifest=json.dumps(manifest))

        with caplog.at_level(logging.ERROR, logger="app.services.package"):
            await fails(service.read_file(None, "docs-skill", "1.0.0", "docs/guide.md"))
            await fails(service.open_raw(None, "docs-skill", "1.0.0", "docs/guide.md"))

        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 2
        assert all("does not match its manifest entry" in r.getMessage() for r in errors)
        untouched = await service.read_file(None, "docs-skill", "1.0.0", "scripts/run.py")
        assert untouched.content == "print('hi')\n"

    async def test_a_wrong_size_in_the_manifest_is_a_storage_error(self, service, db_session, docs):
        manifest = await self.current_manifest(service)
        for entry in manifest:
            if entry["path"] == "scripts/run.py":
                entry["size"] += 1
        await self.tamper(db_session, "docs-skill", "1.0.0", manifest=json.dumps(manifest))

        with pytest.raises(StorageError):
            await service.read_file(None, "docs-skill", "1.0.0", "scripts/run.py")

    async def test_a_manifest_entry_without_a_file_in_the_package_is_a_storage_error(
        self, service, db_session, docs
    ):
        manifest = await self.current_manifest(service)
        manifest.append(
            {
                "path": "ghost.txt",
                "size": 3,
                "sha256": sha("abc"),
                "media_type": "text/plain",
                "text": True,
            }
        )
        await self.tamper(db_session, "docs-skill", "1.0.0", manifest=json.dumps(manifest))

        with pytest.raises(StorageError):
            await service.read_file(None, "docs-skill", "1.0.0", "ghost.txt")
        with pytest.raises(StorageError):
            await service.open_raw(None, "docs-skill", "1.0.0", "ghost.txt")

    async def test_an_overstated_size_is_detected_even_when_the_read_is_truncated(
        self, db_session, blob_store, service, docs
    ):
        manifest = await self.current_manifest(service)
        for entry in manifest:
            if entry["path"] == "scripts/run.py":
                entry["size"] = 100
        await self.tamper(db_session, "docs-skill", "1.0.0", manifest=json.dumps(manifest))
        short = PackageService(db_session, blob_store, max_inline_read_bytes=50)

        with pytest.raises(StorageError):
            await short.read_file(None, "docs-skill", "1.0.0", "scripts/run.py")

    async def test_a_file_missing_from_the_manifest_is_not_served_even_if_it_is_in_the_zip(
        self, service, db_session, docs
    ):
        manifest = [e for e in await self.current_manifest(service) if e["path"] != "big.txt"]
        await self.tamper(db_session, "docs-skill", "1.0.0", manifest=json.dumps(manifest))

        with pytest.raises(NotFoundError):
            await service.read_file(None, "docs-skill", "1.0.0", "big.txt")

    async def test_the_text_flag_comes_from_the_manifest(self, service, db_session, docs):
        manifest = await self.current_manifest(service)
        for entry in manifest:
            if entry["path"] == "docs/guide.md":
                entry["text"] = False
            if entry["path"] == "data/table.bin":
                entry["text"] = True
        await self.tamper(db_session, "docs-skill", "1.0.0", manifest=json.dumps(manifest))

        hidden = await service.read_file(None, "docs-skill", "1.0.0", "docs/guide.md")
        shown = await service.read_file(None, "docs-skill", "1.0.0", "data/table.bin")

        assert hidden.text is False and hidden.content is None
        assert shown.text is True and isinstance(shown.content, str)
        assert shown.size == 256

    @pytest.mark.parametrize(
        "broken",
        [
            "not json at all",
            "{}",
            '"text"',
            "42",
            "null",
            '[{"path": "SKILL.md"}]',
            '["SKILL.md"]',
            '[{"path": "SKILL.md", "size": "many", "sha256": "x", "media_type": "m", "text": true}]',
        ],
    )
    async def test_a_corrupt_manifest_column_is_a_storage_error(
        self, service, db_session, docs, caplog, broken
    ):
        await self.tamper(db_session, "docs-skill", "1.0.0", manifest=broken)

        with caplog.at_level(logging.ERROR, logger="app.services.mappers"):
            await fails(service.manifest(None, "docs-skill", "1.0.0"))
            await fails(service.read_file(None, "docs-skill", "1.0.0", "SKILL.md"))
            await fails(service.entrypoint(None, "docs-skill", "1.0.0"))

        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 3
        assert all("manifest" in r.getMessage() for r in errors)

    @pytest.mark.parametrize("column", ["tags", "metadata_"])
    async def test_corrupt_tag_and_metadata_columns_are_storage_errors(
        self, service, db_session, versions, docs, column
    ):
        await self.tamper(db_session, "docs-skill", "1.0.0", **{column: "{{{"})
        with pytest.raises(StorageError):
            await versions.get(None, "docs-skill", "1.0.0")

    async def test_a_tag_column_of_the_wrong_shape_is_a_storage_error(
        self, service, db_session, versions, docs
    ):
        await self.tamper(db_session, "docs-skill", "1.0.0", tags='["ok", 3]')
        with pytest.raises(StorageError):
            await versions.list(None, "docs-skill")
        await self.tamper(db_session, "docs-skill", "1.0.0", tags='{"a": 1}')
        with pytest.raises(StorageError):
            await versions.list(None, "docs-skill")

    async def test_a_missing_blob_is_a_storage_error_for_every_content_operation(
        self, service, blob_store, docs, caplog
    ):
        await blob_store.delete(docs.version.sha256)

        with caplog.at_level(logging.ERROR, logger="app.services.package"):
            await fails(service.read_file(None, "docs-skill", "1.0.0", "docs/guide.md"))
            await fails(service.open_raw(None, "docs-skill", "1.0.0", "data/table.bin"))
            await fails(service.entrypoint(None, "docs-skill", "latest"))
            await fails(service.open_download(None, "docs-skill", "1.0.0"))

        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 4
        assert all(r.extra_data["sha256"] == docs.version.sha256 for r in errors)

    async def test_metadata_only_operations_survive_a_missing_blob(self, service, blob_store, docs):
        await blob_store.delete(docs.version.sha256)

        listing = await service.manifest(None, "docs-skill", "1.0.0")
        info = await service.download_info(None, "docs-skill", "1.0.0")
        binary = await service.read_file(None, "docs-skill", "1.0.0", "data/table.bin")

        assert len(listing.files) == len(FILES) + 1
        assert info.sha256 == docs.version.sha256
        assert binary.content is None

    async def test_a_corrupted_blob_is_a_storage_error(self, service, blob_store, docs):
        path = blob_path(blob_store, docs.version.sha256)
        path.write_bytes(corrupt_entry_data(path.read_bytes(), "docs/guide.md"))

        with pytest.raises(StorageError):
            await service.read_file(None, "docs-skill", "1.0.0", "docs/guide.md")
        untouched = await service.read_file(None, "docs-skill", "1.0.0", "scripts/run.py")
        assert untouched.content == "print('hi')\n"

    async def test_a_blob_that_is_not_a_zip_is_a_storage_error(self, service, blob_store, docs):
        blob_path(blob_store, docs.version.sha256).write_bytes(b"garbage")
        with pytest.raises(StorageError):
            await service.read_file(None, "docs-skill", "1.0.0", "docs/guide.md")

    async def test_a_tampered_root_prefix_is_a_storage_error(self, service, db_session, docs):
        await self.tamper(db_session, "docs-skill", "1.0.0", root_prefix="../")
        with pytest.raises(StorageError):
            await service.read_file(None, "docs-skill", "1.0.0", "docs/guide.md")

    @pytest.mark.parametrize("prefix", ["../", "pkg", "a/b/", "/", "x\\", "wrong//"])
    async def test_a_tampered_root_prefix_reaches_the_client_only_as_a_generic_storage_error(
        self, service, db_session, docs, caplog, prefix
    ):
        await self.tamper(db_session, "docs-skill", "1.0.0", root_prefix=prefix)

        with caplog.at_level(logging.ERROR, logger="app.services.package"):
            errors = [
                await fails(service.read_file(None, "docs-skill", "1.0.0", "docs/guide.md")),
                await fails(service.open_raw(None, "docs-skill", "1.0.0", "docs/guide.md")),
                await fails(service.entrypoint(None, "docs-skill", "1.0.0")),
            ]

        for error in errors:
            assert type(error) is StorageError
            assert error.message == "The stored package cannot be read"
            assert (error.code, error.reason) == ("STORAGE_ERROR", None)
        logged = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert len(logged) == 3
        assert all("Invalid root prefix" in message for message in logged)

    async def test_a_negative_inline_limit_is_reported_as_a_storage_error_and_only_for_inline_reads(
        self, db_session, blob_store, docs
    ):
        broken = PackageService(
            db_session,
            blob_store,
            public_read=True,
            auto_publish=False,
            max_inline_read_bytes=-1,
            public_url=PUBLIC_URL,
        )

        error = await fails(broken.read_file(None, "docs-skill", "1.0.0", "docs/guide.md"))

        assert type(error) is StorageError
        assert error.message == "The stored package cannot be read"
        raw = await broken.open_raw(None, "docs-skill", "1.0.0", "docs/guide.md")
        assert b"".join(await collect(raw.stream)) == FILES["docs/guide.md"].encode("utf-8")
        binary = await broken.read_file(None, "docs-skill", "1.0.0", "data/table.bin")
        assert binary.content is None

    async def test_a_blob_key_that_is_not_a_hash_is_a_storage_error(
        self, service, db_session, docs
    ):
        await self.tamper(db_session, "docs-skill", "1.0.0", blob_sha256="../../etc/passwd")
        with pytest.raises(StorageError):
            await service.read_file(None, "docs-skill", "1.0.0", "docs/guide.md")
        with pytest.raises(StorageError):
            await service.open_download(None, "docs-skill", "1.0.0")


class TestDownload:
    async def test_info_describes_the_package_and_its_url(self, service, actors, docs):
        info = await service.download_info(None, "docs-skill", "latest")

        assert isinstance(info, DownloadInfo)
        assert info.name == "docs-skill"
        assert info.version == "1.0.0"
        assert info.sha256 == docs.version.sha256
        assert info.size_bytes == docs.version.size_bytes
        assert info.url == f"{PUBLIC_URL}/api/v1/skills/docs-skill/versions/1.0.0/download"
        assert info.requires_auth is False

    async def test_streams_the_exact_bytes_with_the_advertised_hash(self, service, actors, docs):
        original = make_skill_zip("docs-skill", "1.0.0", files=FILES)

        download = await service.open_download(None, "docs-skill", "1.0.0")
        chunks = await collect(download.stream)

        assert isinstance(download, PackageDownload)
        assert download.filename == "docs-skill-1.0.0.zip"
        assert download.info == await service.download_info(None, "docs-skill", "1.0.0")
        assert b"".join(chunks) == original
        assert sha(b"".join(chunks)) == download.info.sha256
        assert len(b"".join(chunks)) == download.info.size_bytes

    async def test_large_packages_stream_in_several_chunks(self, service, actors):
        data = make_skill_zip("bulky", "1.0.0", files={"payload.bin": RANDOM_BLOB})
        await service.upload(actors.owner, data, publish=True)

        download = await service.open_download(None, "bulky", "latest")
        chunks = await collect(download.stream)

        assert len(chunks) > 1
        assert b"".join(chunks) == data
        assert sha(b"".join(chunks)) == download.info.sha256

    async def test_latest_resolves_to_the_newest_published_version(self, service, actors, docs):
        await service.upload(
            actors.owner, make_skill_zip("docs-skill", "1.1.0", files=FILES), publish=True
        )
        download = await service.open_download(None, "docs-skill", "latest")
        assert download.filename == "docs-skill-1.1.0.zip"
        await download.stream.aclose()

    async def test_a_deprecated_version_is_still_downloadable_by_version(
        self, service, versions, actors, docs
    ):
        await service.upload(
            actors.owner, make_skill_zip("docs-skill", "1.1.0", files=FILES), publish=True
        )
        await versions.deprecate(actors.owner, "docs-skill", "1.0.0")

        old = await service.open_download(None, "docs-skill", "1.0.0")
        assert b"".join(await collect(old.stream)) == make_skill_zip(
            "docs-skill", "1.0.0", files=FILES
        )
        latest = await service.download_info(None, "docs-skill", "latest")
        assert latest.version == "1.1.0"

    async def test_public_published_downloads_do_not_require_auth(self, service, actors, docs):
        assert (await service.download_info(None, "docs-skill", "1.0.0")).requires_auth is False
        assert (
            await service.download_info(actors.owner, "docs-skill", "1.0.0")
        ).requires_auth is False

    async def test_a_private_registry_always_requires_auth(self, private_service, actors, docs):
        info = await private_service.download_info(actors.stranger, "docs-skill", "1.0.0")
        assert info.requires_auth is True

    async def test_draft_versions_require_auth(self, service, actors, docs):
        await service.upload(
            actors.owner, make_skill_zip("docs-skill", "1.1.0", files=FILES), publish=False
        )
        info = await service.download_info(actors.owner, "docs-skill", "1.1.0")
        assert info.requires_auth is True
        download = await service.open_download(actors.owner, "docs-skill", "1.1.0")
        assert download.info.requires_auth is True
        await download.stream.aclose()

    async def test_a_missing_blob_fails_when_the_download_is_opened(
        self, service, blob_store, docs, caplog
    ):
        await blob_store.delete(docs.version.sha256)
        with (
            caplog.at_level(logging.ERROR, logger="app.services.package"),
            pytest.raises(StorageError),
        ):
            await service.open_download(None, "docs-skill", "1.0.0")
        assert any("unavailable" in r.getMessage() for r in caplog.records)

    async def test_a_blob_of_the_wrong_size_fails_when_the_download_is_opened(
        self, service, blob_store, docs
    ):
        path = blob_path(blob_store, docs.version.sha256)
        path.write_bytes(path.read_bytes()[:-5])
        with pytest.raises(StorageError, match="size"):
            await service.open_download(None, "docs-skill", "1.0.0")

    async def test_a_corrupted_blob_of_the_right_size_is_flagged_at_the_end_of_the_stream(
        self, service, blob_store, docs, caplog
    ):
        path = blob_path(blob_store, docs.version.sha256)
        content = bytearray(path.read_bytes())
        content[10] ^= 0xFF
        path.write_bytes(bytes(content))

        download = await service.open_download(None, "docs-skill", "1.0.0")
        received = bytearray()
        with (
            caplog.at_level(logging.ERROR, logger="app.services.package"),
            pytest.raises(StorageError, match="hash"),
        ):
            async for chunk in download.stream:
                received += chunk

        assert bytes(received) == bytes(content)
        assert any("does not match its hash" in r.getMessage() for r in caplog.records)

    async def test_a_blob_that_disappears_after_opening_fails_while_streaming(
        self, service, blob_store, docs
    ):
        download = await service.open_download(None, "docs-skill", "1.0.0")
        await blob_store.delete(docs.version.sha256)
        with pytest.raises(StorageError, match="unavailable"):
            await collect(download.stream)

    async def test_abandoning_a_stream_early_releases_it_quietly(self, service, actors):
        data = make_skill_zip("abandon", "1.0.0", files={"payload.bin": RANDOM_BLOB})
        await service.upload(actors.owner, data, publish=True)

        download = await service.open_download(None, "abandon", "1.0.0")
        first = await download.stream.__anext__()
        await download.stream.aclose()

        assert data.startswith(first)

    @pytest.fixture
    async def recorded(self, db_session, blob_store, actors):
        recording = RecordingStore(blob_store, chunk_size=64)
        recorded_service = PackageService(
            db_session, recording, public_read=True, auto_publish=False, public_url=PUBLIC_URL
        )
        data = make_skill_zip("abandon", "1.0.0", files={"payload.bin": RANDOM_BLOB[:8192]})
        await recorded_service.upload(actors.owner, data, publish=True)
        return recorded_service, recording, data

    async def test_abandoning_a_stream_closes_the_inner_generator_before_aclose_returns(
        self, recorded
    ):
        recorded_service, recording, data = recorded
        download = await recorded_service.open_download(None, "abandon", "1.0.0")
        sha256 = download.info.sha256

        first = await download.stream.__anext__()
        assert recording.opened == [sha256]
        assert recording.closed == []
        await download.stream.aclose()

        assert recording.closed == [sha256]
        assert data.startswith(first)

    async def test_breaking_out_of_the_loop_and_closing_releases_the_inner_generator(
        self, recorded
    ):
        recorded_service, recording, _ = recorded
        download = await recorded_service.open_download(None, "abandon", "1.0.0")

        received = 0
        async for chunk in download.stream:
            received += len(chunk)
            if received >= 200:
                break
        await download.stream.aclose()

        assert recording.closed == [download.info.sha256]

    async def test_a_consumer_that_raises_still_releases_the_inner_generator(self, recorded):
        recorded_service, recording, _ = recorded
        download = await recorded_service.open_download(None, "abandon", "1.0.0")

        with pytest.raises(RuntimeError, match="client disconnected"):
            try:
                async for _ in download.stream:
                    raise RuntimeError("client disconnected")
            finally:
                await download.stream.aclose()

        assert recording.closed == [download.info.sha256]

    async def test_a_fully_consumed_stream_closes_the_inner_generator_exactly_once(self, recorded):
        recorded_service, recording, data = recorded
        download = await recorded_service.open_download(None, "abandon", "1.0.0")

        chunks = await collect(download.stream)
        await download.stream.aclose()

        assert b"".join(chunks) == data
        assert len(chunks) > 1
        assert recording.opened == recording.closed == [download.info.sha256]

    async def test_a_stream_that_was_never_started_never_opens_the_store(self, recorded):
        recorded_service, recording, _ = recorded
        download = await recorded_service.open_download(None, "abandon", "1.0.0")

        await download.stream.aclose()

        assert recording.opened == []
        assert recording.closed == []

    async def test_a_store_failure_in_the_middle_is_a_storage_error_and_releases_the_generator(
        self, db_session, blob_store, actors
    ):
        failing = RecordingStore(blob_store, chunk_size=64, fail_after=3)
        service = PackageService(
            db_session, failing, public_read=True, auto_publish=False, public_url=PUBLIC_URL
        )
        data = make_skill_zip("flaky", "1.0.0", files={"payload.bin": RANDOM_BLOB[:8192]})
        await service.upload(actors.owner, data, publish=True)
        download = await service.open_download(None, "flaky", "1.0.0")

        with pytest.raises(StorageError, match="unavailable"):
            await collect(download.stream)

        assert failing.closed == [download.info.sha256]

    async def test_a_corrupted_blob_still_closes_the_inner_generator(self, recorded, blob_store):
        recorded_service, recording, _ = recorded
        download = await recorded_service.open_download(None, "abandon", "1.0.0")
        path = blob_path(blob_store, download.info.sha256)
        content = bytearray(path.read_bytes())
        content[20] ^= 0xFF
        path.write_bytes(bytes(content))

        with pytest.raises(StorageError, match="hash"):
            await collect(download.stream)

        assert recording.closed == [download.info.sha256]

    async def test_the_stream_does_not_depend_on_the_database_session(
        self, service, db_session, actors, docs
    ):
        download = await service.open_download(None, "docs-skill", "1.0.0")
        await db_session.close()
        chunks = await collect(download.stream)
        assert sha(b"".join(chunks)) == download.info.sha256


TARGETS = [
    ("docs-skill", "1.0.0", frozenset(ACTOR_NAMES)),
    ("docs-skill", "latest", frozenset(ACTOR_NAMES)),
    ("docs-skill", "0.9.0", frozenset(ACTOR_NAMES)),
    ("docs-skill", "1.1.0", PRIVILEGED_ACTORS),
    ("hidden-docs", "1.0.0", PRIVILEGED_ACTORS),
    ("hidden-docs", "latest", PRIVILEGED_ACTORS),
    ("draft-docs", "0.1.0", PRIVILEGED_ACTORS),
    ("draft-docs", "latest", frozenset()),
    ("nothing-here", "latest", frozenset()),
]


@pytest_asyncio.fixture
async def library(service, versions, skills, actors):
    owner = actors.owner
    await upload_package(service, owner, "docs-skill", "0.9.0", files=FILES, publish=True)
    await upload_package(service, owner, "docs-skill", "1.0.0", files=FILES, publish=True)
    await upload_package(service, owner, "docs-skill", "1.1.0", files=FILES, publish=False)
    await versions.deprecate(owner, "docs-skill", "0.9.0")
    await upload_package(service, owner, "hidden-docs", "1.0.0", kind="grounding", publish=True)
    await skills.hide(owner, "hidden-docs")
    await upload_package(service, owner, "draft-docs", "0.1.0", publish=False)


def entry_path(ref: str) -> str:
    return "INDEX.md" if ref == "hidden-docs" else "SKILL.md"


CONTENT_OPERATIONS = {
    "manifest": lambda svc, who, ref, ver: svc.manifest(who, ref, ver),
    "entrypoint": lambda svc, who, ref, ver: svc.entrypoint(who, ref, ver),
    "read_file": lambda svc, who, ref, ver: svc.read_file(who, ref, ver, entry_path(ref)),
    "open_raw": lambda svc, who, ref, ver: svc.open_raw(who, ref, ver, entry_path(ref)),
    "download_info": lambda svc, who, ref, ver: svc.download_info(who, ref, ver),
    "open_download": lambda svc, who, ref, ver: svc.open_download(who, ref, ver),
}


async def observe(call, service, principal, ref, version) -> str:
    try:
        result = await call(service, principal, ref, version)
    except NotFoundError:
        return "not-found"
    except UnauthorizedError:
        return "unauthorized"
    stream = getattr(result, "stream", None)
    if stream is not None:
        await stream.aclose()
    return "ok"


class TestReadAuthorizationMatrix:
    @pytest.mark.parametrize("operation", list(CONTENT_OPERATIONS))
    async def test_visibility_follows_the_catalog_rules(self, service, library, actors, operation):
        call = CONTENT_OPERATIONS[operation]
        mismatches = []
        for ref, version, allowed in TARGETS:
            for actor in ACTOR_NAMES:
                expected = "ok" if actor in allowed else "not-found"
                seen = await observe(call, service, actors.get(actor), ref, version)
                if seen != expected:
                    mismatches.append((operation, actor, f"{ref}@{version}", seen, expected))
        assert mismatches == []

    @pytest.mark.parametrize("operation", list(CONTENT_OPERATIONS))
    async def test_private_registries_reject_anonymous_readers_before_anything_else(
        self, private_service, library, actors, operation
    ):
        call = CONTENT_OPERATIONS[operation]
        for ref, version, _ in TARGETS:
            assert await observe(call, private_service, None, ref, version) == "unauthorized"

    @pytest.mark.parametrize("operation", list(CONTENT_OPERATIONS))
    async def test_private_registries_still_serve_authenticated_readers(
        self, private_service, library, actors, operation
    ):
        call = CONTENT_OPERATIONS[operation]
        reader = actors.stranger_read
        assert await observe(call, private_service, reader, "docs-skill", "1.0.0") == "ok"
        assert await observe(call, private_service, reader, "hidden-docs", "1.0.0") == "not-found"
        assert await observe(call, private_service, actors.owner, "hidden-docs", "1.0.0") == "ok"

    async def test_invisible_files_do_not_reveal_whether_the_path_exists(
        self, service, library, actors
    ):
        with pytest.raises(NotFoundError) as real:
            await service.read_file(actors.stranger, "docs-skill", "1.1.0", "SKILL.md")
        with pytest.raises(NotFoundError) as imaginary:
            await service.read_file(actors.stranger, "docs-skill", "1.1.0", "nope.txt")
        assert real.value.code == imaginary.value.code == "NOT_FOUND"
        assert real.value.message == imaginary.value.message

    async def test_download_info_marks_the_draft_version_as_requiring_auth(
        self, service, library, actors
    ):
        draft = await service.download_info(actors.owner, "docs-skill", "1.1.0")
        published = await service.download_info(actors.owner, "docs-skill", "1.0.0")
        assert (draft.requires_auth, published.requires_auth) == (True, False)

    @pytest.mark.parametrize("actor", ["owner", "admin"])
    @pytest.mark.parametrize("version", ["1.0.0", "latest"])
    async def test_download_info_marks_a_hidden_entry_as_requiring_auth(
        self, service, library, actors, actor, version
    ):
        info = await service.download_info(actors.get(actor), "hidden-docs", version)
        download = await service.open_download(actors.get(actor), "hidden-docs", version)
        await download.stream.aclose()

        assert info.version == "1.0.0"
        assert info.requires_auth is True
        assert download.info.requires_auth is True

    async def test_a_restored_entry_stops_requiring_auth(self, service, skills, library, actors):
        assert (await service.download_info(actors.owner, "hidden-docs")).requires_auth is True

        await skills.restore(actors.owner, "hidden-docs")

        assert (await service.download_info(actors.owner, "hidden-docs")).requires_auth is False
        assert (await service.download_info(None, "hidden-docs")).requires_auth is False

    async def test_hiding_a_published_entry_makes_its_downloads_private(
        self, service, skills, library, actors
    ):
        assert (await service.download_info(actors.owner, "docs-skill")).requires_auth is False

        await skills.hide(actors.owner, "docs-skill")

        for number in ("1.0.0", "latest"):
            info = await service.download_info(actors.owner, "docs-skill", number)
            assert info.requires_auth is True


def expected_requires_auth(public_read, skill_status, version_status):
    return not (
        public_read
        and skill_status == SkillStatus.PUBLISHED
        and version_status != VersionStatus.DRAFT
    )


class TestDownloadRequiresAuthMatrix:
    @pytest.mark.parametrize("public_read", [True, False], ids=["public-read", "private-read"])
    @pytest.mark.parametrize("skill_status", list(SkillStatus))
    @pytest.mark.parametrize("version_status", list(VersionStatus))
    async def test_the_flag_is_true_unless_an_anonymous_reader_could_fetch_the_file(
        self, db_session, blob_store, actors, public_read, skill_status, version_status
    ):
        latest = "1.0.0" if skill_status == SkillStatus.PUBLISHED else None
        skill_id = await add_skill(
            db_session, actors.owner, "matrix-skill", status=skill_status, latest_version=latest
        )
        await add_version(db_session, skill_id, "1.0.0", actors.owner, status=version_status)
        service = PackageService(db_session, blob_store, public_read=public_read)

        info = await service.download_info(actors.owner, "matrix-skill", "1.0.0")

        assert info.requires_auth is expected_requires_auth(
            public_read, skill_status, version_status
        )


class TestSessionExpiry:
    async def test_services_never_read_attributes_after_the_commit(self, engine, blob_store):
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=True)
        async with factory() as setup:
            owner = (await make_actors(setup)).owner
        async with factory() as db:
            packages = PackageService(db, blob_store, public_read=True, auto_publish=False)
            versions = VersionService(db, public_read=True)
            skills = SkillService(db, public_read=True)

            first = await packages.upload(
                owner,
                make_skill_zip("expiring", "1.0.0", tags=["a"], files={"x.txt": "x"}),
                publish=True,
            )
            second = await packages.upload(owner, make_skill_zip("expiring", "1.1.0", tags=["b"]))
            published = await versions.publish(owner, "expiring", "1.1.0")
            deprecated = await versions.deprecate(owner, "expiring", "1.0.0")
            await skills.hide(owner, "expiring")
            restored = await skills.restore(owner, "expiring")
            listing = await skills.search(None, tags=["b"])
            content = await packages.read_file(None, "expiring", "latest", "SKILL.md")
            download = await packages.open_download(None, "expiring", "1.0.0")
            received = b"".join([chunk async for chunk in download.stream])

        assert (first.version.status, second.version.status) == ("published", "draft")
        assert (published.status, deprecated.status) == ("published", "deprecated")
        assert (restored.status, restored.latest_version, restored.tags) == (
            "published",
            "1.1.0",
            ["b"],
        )
        assert [item.name for item in listing.items] == ["expiring"]
        assert content.text is True and "expiring" in content.content
        assert sha(received) == first.version.sha256


class TestEndToEndWithDifferentUsers:
    async def test_a_second_user_cannot_overwrite_or_shadow_anything(
        self, db_session, service, actors, blob_store, session_factory
    ):
        mallory = await make_principal(db_session, "mallory", scope=TokenScope.WRITE)
        data = make_skill_zip("victim", "1.0.0", description="legit")
        await service.upload(actors.owner, data, publish=True)

        with pytest.raises(ForbiddenError):
            await service.upload(mallory, make_skill_zip("victim", "1.0.1", description="evil"))
        with pytest.raises(ForbiddenError):
            await service.upload(mallory, make_skill_zip("victim", "1.0.0", description="evil"))

        stored = await stored_version(session_factory, "victim", "1.0.0")
        assert stored.blob_sha256 == sha(data)
        assert await row_counts(session_factory) == (1, 1)
        assert stored_blobs(blob_store) == [sha(data)]
        content = await service.entrypoint(None, "victim", "latest")
        assert "legit" in content.content

    async def test_inspector_results_match_what_the_service_stores(
        self, service, actors, session_factory
    ):
        data = make_skill_zip(
            "faithful",
            "3.2.1",
            description="Exactly as parsed",
            tags=["x", "y"],
            category="cat",
            files={"a/b.txt": "ab", "c.bin": b"\x00\xff"},
        )
        parsed = inspect_package(data)

        await service.upload(actors.owner, data, publish=True)

        stored = await stored_version(session_factory, "faithful", "3.2.1")
        assert stored.blob_sha256 == parsed.sha256
        assert stored.size_bytes == parsed.size_bytes
        assert stored.uncompressed_bytes == parsed.uncompressed_bytes
        assert stored.file_count == parsed.file_count
        assert stored.entrypoint == parsed.entrypoint
        assert stored.root_prefix == parsed.root_prefix
        assert stored.search_text == parsed.search_text
        assert stored.status == VersionStatus.PUBLISHED
