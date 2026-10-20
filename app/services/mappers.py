from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any
from urllib.parse import quote

from app.logging import get_logger
from app.models.schemas import (
    ManifestEntryResponse,
    SkillDetail,
    SkillSummary,
    VersionDetail,
    VersionSummary,
)
from app.models.skill import Skill, SkillKind, SkillStatus, SkillVersion, VersionStatus
from app.services.errors import StorageError

logger = get_logger("app.services.mappers")

API_PREFIX = "/api/v1"


def download_url(public_url: str, name: str, version: str) -> str:
    base = public_url.rstrip("/")
    return (
        f"{base}{API_PREFIX}/skills/{quote(name, safe='')}"
        f"/versions/{quote(version, safe='')}/download"
    )


def _corrupt(version: SkillVersion, column: str, detail: str) -> StorageError:
    logger.error(
        f"Stored {column} of version {version.id} is corrupt: {detail}",
        extra={"extra_data": {"version_id": version.id, "column": column}},
    )
    return StorageError(f"The stored {column} of this version is corrupt")


def _load_json(version: SkillVersion, column: str, raw: str, expected: type) -> Any:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise _corrupt(version, column, "not valid JSON") from exc
    if not isinstance(value, expected):
        raise _corrupt(version, column, f"expected a JSON {expected.__name__}")
    return value


def version_tags(version: SkillVersion) -> list[str]:
    tags = _load_json(version, "tags", version.tags, list)
    if not all(isinstance(tag, str) for tag in tags):
        raise _corrupt(version, "tags", "expected a list of strings")
    return tags


def version_metadata(version: SkillVersion) -> dict[str, Any]:
    return _load_json(version, "metadata", version.metadata_, dict)


def manifest_entries(version: SkillVersion) -> list[ManifestEntryResponse]:
    items = _load_json(version, "manifest", version.manifest, list)
    try:
        return [ManifestEntryResponse.model_validate(item) for item in items]
    except ValueError as exc:
        raise _corrupt(version, "manifest", "an entry has the wrong shape") from exc


def skill_summary(skill: Skill) -> SkillSummary:
    return SkillSummary(
        id=skill.id,
        name=skill.name,
        display_name=skill.display_name,
        description=skill.description,
        kind=SkillKind(skill.kind).value,
        category=skill.category,
        tags=list(skill.tags),
        status=SkillStatus(skill.status).value,
        latest_version=skill.latest_version,
        owner_id=skill.owner_id,
        created_at=skill.created_at,
        updated_at=skill.updated_at,
    )


def version_summary(version: SkillVersion) -> VersionSummary:
    return VersionSummary(
        id=version.id,
        skill_id=version.skill_id,
        version=version.version,
        status=VersionStatus(version.status).value,
        sha256=version.blob_sha256,
        size_bytes=version.size_bytes,
        uncompressed_bytes=version.uncompressed_bytes,
        file_count=version.file_count,
        entrypoint=version.entrypoint,
        display_name=version.display_name,
        description=version.description,
        category=version.category,
        tags=version_tags(version),
        uploaded_by=version.uploaded_by,
        published_by=version.published_by,
        published_at=version.published_at,
        deprecated_at=version.deprecated_at,
        created_at=version.created_at,
    )


def version_detail(version: SkillVersion, *, url: str) -> VersionDetail:
    summary = version_summary(version)
    return VersionDetail(
        **summary.model_dump(),
        metadata=version_metadata(version),
        download_url=url,
    )


def skill_detail(skill: Skill, versions: Sequence[SkillVersion]) -> SkillDetail:
    summary = skill_summary(skill)
    return SkillDetail(
        **summary.model_dump(),
        versions=[version_summary(item) for item in versions],
    )
