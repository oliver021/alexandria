from __future__ import annotations

import enum
import uuid
from collections.abc import Iterable, Sequence
from typing import TypeVar

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.skill import Skill, SkillKind, SkillStatus, SkillVersion, VersionStatus
from app.repositories import skill_repo
from app.services import access, mappers, semver
from app.services.errors import NotFoundError, ValidationError
from app.services.package_inspector import looks_like_uuid
from app.services.principal import Principal
from app.text import is_valid_text

LATEST = "latest"
MAX_PAGE_SIZE = 100
MAX_TAXONOMY_SIZE = 200
MAX_OFFSET = 2**31 - 1
MAX_QUERY_LENGTH = 512
MAX_FILTER_LENGTH = 256
_PREVIEW_LENGTH = 80

EnumT = TypeVar("EnumT", bound=enum.Enum)


def shorten(value: object) -> str:
    text = value if isinstance(value, str) else repr(value)
    if len(text) > _PREVIEW_LENGTH:
        text = text[:_PREVIEW_LENGTH] + "..."
    return repr(text)


def _enum_filter(enum_class: type[EnumT], value: object, field: str) -> EnumT | None:
    if value is None:
        return None
    if isinstance(value, enum_class):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if not text:
            return None
        try:
            return enum_class(text)
        except ValueError:
            pass
    allowed = ", ".join(repr(member.value) for member in enum_class)
    raise ValidationError(f"Invalid {field} {shorten(value)}: expected one of {allowed}")


def parse_kind(value: object) -> SkillKind | None:
    return _enum_filter(SkillKind, value, "kind")


def parse_skill_status(value: object) -> SkillStatus | None:
    return _enum_filter(SkillStatus, value, "status")


def check_sort(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        if not text:
            return None
        if text in skill_repo.SORT_OPTIONS:
            return text
    allowed = ", ".join(repr(option) for option in skill_repo.SORT_OPTIONS)
    raise ValidationError(f"Invalid sort {shorten(value)}: expected one of {allowed}")


def check_limit(value: object, *, maximum: int = MAX_PAGE_SIZE) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValidationError(f"limit must be an integer between 1 and {maximum}")
    return value


def check_offset(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_OFFSET:
        raise ValidationError(f"offset must be an integer between 0 and {MAX_OFFSET}")
    return value


def _check_text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    if not is_valid_text(value):
        raise ValidationError(f"{field} must be valid UTF-8 text without NUL characters")
    if len(value) > maximum:
        raise ValidationError(f"{field} must have at most {maximum} characters")
    return value


def clean_query(value: object) -> str | None:
    if value is None:
        return None
    return _check_text(value, "q", MAX_QUERY_LENGTH)


def clean_category(value: object) -> str | None:
    if value is None:
        return None
    return _check_text(value, "category", MAX_FILTER_LENGTH)


def clean_tags(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (_check_text(value, "tag", MAX_FILTER_LENGTH),)
    try:
        items = tuple(value)
    except TypeError as exc:
        raise ValidationError("tags must be a list of strings") from exc
    return tuple(_check_text(item, "tag", MAX_FILTER_LENGTH) for item in items)


async def find_skill(db: AsyncSession, ref: object) -> Skill | None:
    if not is_valid_text(ref) or not ref:
        return None
    if looks_like_uuid(ref):
        return await skill_repo.get_skill(db, str(uuid.UUID(ref)))
    return await skill_repo.get_skill_by_name(db, ref)


async def get_visible_skill(db: AsyncSession, principal: Principal | None, ref: object) -> Skill:
    skill = await find_skill(db, ref)
    if skill is None or not access.can_view_skill(principal, skill):
        raise NotFoundError(f"Skill not found: {shorten(ref)}")
    return skill


async def get_manageable_skill(
    db: AsyncSession, principal: Principal | None, ref: object
) -> tuple[Skill, Principal]:
    access.require_write(principal)
    skill = await get_visible_skill(db, principal, ref)
    writer = access.require_manage(principal, skill)
    return skill, writer


def viewer(principal: Principal | None) -> tuple[str | None, bool]:
    if principal is None:
        return None, False
    return principal.user_id, principal.is_admin


def sort_versions_desc(versions: Iterable[SkillVersion]) -> list[SkillVersion]:
    return sorted(versions, key=lambda item: semver.sort_key(item.version), reverse=True)


async def list_visible_versions(
    db: AsyncSession, principal: Principal | None, skill: Skill
) -> list[SkillVersion]:
    statuses = None
    if not access.is_owner_or_admin(principal, skill.owner_id):
        statuses = access.PUBLIC_VERSION_STATUSES
    versions = await skill_repo.list_versions(db, skill.id, statuses=statuses)
    return sort_versions_desc(versions)


async def get_exact_version(
    db: AsyncSession, principal: Principal | None, skill: Skill, version: object
) -> SkillVersion:
    if not is_valid_text(version) or not version or len(version) > semver.MAX_VERSION_LENGTH:
        raise NotFoundError(f"Version not found: {shorten(version)}")
    found = await skill_repo.get_version(db, skill.id, version)
    if found is None or not access.can_view_version(principal, skill, found):
        raise NotFoundError(f"Version not found: {shorten(version)}")
    return found


async def get_latest_version(db: AsyncSession, skill: Skill) -> SkillVersion:
    published = await skill_repo.list_versions(db, skill.id, statuses=(VersionStatus.PUBLISHED,))
    number = semver.pick_latest(item.version for item in published)
    if number is None:
        raise NotFoundError("The entry has no published version")
    latest = await skill_repo.get_version(db, skill.id, number)
    if latest is None:
        raise NotFoundError("The entry has no published version")
    return latest


async def resolve_version(
    db: AsyncSession, principal: Principal | None, skill: Skill, version: object
) -> SkillVersion:
    if version == LATEST:
        return await get_latest_version(db, skill)
    return await get_exact_version(db, principal, skill, version)


def _pick_source(
    versions: Sequence[SkillVersion], published: Sequence[SkillVersion], latest: str | None
) -> SkillVersion | None:
    if latest is not None:
        return next(item for item in published if item.version == latest)
    if versions:
        return max(versions, key=lambda item: semver.sort_key(item.version))
    return None


async def refresh_skill(db: AsyncSession, skill: Skill, *, reopen: bool = False) -> Skill:
    versions = await skill_repo.list_versions(db, skill.id)
    published = [item for item in versions if item.status == VersionStatus.PUBLISHED]
    latest = semver.pick_latest(item.version for item in published)
    if skill.status == SkillStatus.UNPUBLISHED and not reopen:
        status = SkillStatus.UNPUBLISHED
    else:
        status = SkillStatus.PUBLISHED if latest is not None else SkillStatus.DRAFT
    source = _pick_source(versions, published, latest)
    if source is None:
        return await skill_repo.set_skill_status(db, skill, status)
    search_text = await skill_repo.get_version_search_text(db, source.id)
    return await skill_repo.update_skill_denormalized(
        db,
        skill,
        display_name=source.display_name,
        description=source.description,
        category=source.category,
        search_text=search_text,
        latest_version=latest,
        tags=mappers.version_tags(source),
        status=status,
    )
