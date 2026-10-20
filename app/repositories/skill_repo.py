from __future__ import annotations

import json
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import reduce
from operator import add
from typing import Any

from sqlalchemy import ColumnElement, and_, case, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute, defer

from app.models.skill import Skill, SkillKind, SkillStatus, SkillTag, SkillVersion, VersionStatus

LIKE_ESCAPE = "\\"
SORT_OPTIONS = ("relevance", "name", "updated", "created")
MAX_SEARCH_TERMS = 10
MAX_TAG_FILTERS = 20
MUTABLE_VERSION_FIELDS = frozenset({"status", "published_by", "published_at", "deprecated_at"})
HEAVY_VERSION_COLUMNS = (SkillVersion.manifest, SkillVersion.search_text, SkillVersion.metadata_)

SCORE_NAME_EXACT = 100
SCORE_NAME_PREFIX = 60
SCORE_NAME_CONTAINS = 40
SCORE_DISPLAY_NAME = 30
SCORE_TAG_EXACT = 25
SCORE_DESCRIPTION = 10
SCORE_SEARCH_TEXT = 3


@dataclass(frozen=True, slots=True)
class SkillQuery:
    q: str | None = None
    kind: SkillKind | None = None
    tags: Sequence[str] = ()
    category: str | None = None
    owner_id: str | None = None
    statuses: Collection[SkillStatus] | None = None
    viewer_id: str | None = None
    viewer_is_admin: bool = False
    sort: str | None = None
    limit: int = 20
    offset: int = 0


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def split_terms(q: str | None) -> list[str]:
    if not q:
        return []
    terms: dict[str, None] = {}
    for raw in q.split():
        term = raw.replace("\x00", "").lower()
        if term:
            terms.setdefault(term, None)
    return list(terms)[:MAX_SEARCH_TERMS]


def normalize_tags(tags: Iterable[str]) -> list[str]:
    unique: dict[str, None] = {}
    for tag in tags:
        value = tag.strip().lower()
        if value:
            unique.setdefault(value, None)
    return list(unique)


def _contains(column: InstrumentedAttribute[Any], pattern: str) -> ColumnElement[bool]:
    return func.lower(column).like(pattern, escape=LIKE_ESCAPE)


def _has_tag(tag: str) -> ColumnElement[bool]:
    return exists().where(SkillTag.skill_id == Skill.id, SkillTag.tag == tag).correlate(Skill)


def _term_matches(term: str) -> ColumnElement[bool]:
    pattern = f"%{escape_like(term)}%"
    return or_(
        _contains(Skill.name, pattern),
        _contains(Skill.display_name, pattern),
        _contains(Skill.description, pattern),
        _contains(Skill.search_text, pattern),
        exists()
        .where(SkillTag.skill_id == Skill.id, _contains(SkillTag.tag, pattern))
        .correlate(Skill),
    )


def _term_score(term: str) -> ColumnElement[int]:
    escaped = escape_like(term)
    contains = f"%{escaped}%"
    lowered_name = func.lower(Skill.name)
    name_score = case(
        (lowered_name == term, SCORE_NAME_EXACT),
        (lowered_name.like(f"{escaped}%", escape=LIKE_ESCAPE), SCORE_NAME_PREFIX),
        (lowered_name.like(contains, escape=LIKE_ESCAPE), SCORE_NAME_CONTAINS),
        else_=0,
    )
    parts = [
        name_score,
        case((_contains(Skill.display_name, contains), SCORE_DISPLAY_NAME), else_=0),
        case((_has_tag(term), SCORE_TAG_EXACT), else_=0),
        case((_contains(Skill.description, contains), SCORE_DESCRIPTION), else_=0),
        case((_contains(Skill.search_text, contains), SCORE_SEARCH_TEXT), else_=0),
    ]
    return reduce(add, parts)


def visibility_clause(viewer_id: str | None, viewer_is_admin: bool) -> ColumnElement[bool] | None:
    if viewer_is_admin:
        return None
    public = and_(Skill.status == SkillStatus.PUBLISHED, Skill.latest_version.is_not(None))
    if viewer_id is None:
        return public
    return or_(public, Skill.owner_id == viewer_id)


def _resolve_sort(sort: str | None, has_terms: bool) -> str:
    if sort is None:
        return "relevance" if has_terms else "name"
    if sort not in SORT_OPTIONS:
        raise ValueError(f"Unknown sort: {sort}")
    return sort


def _ordering(sort: str, terms: Sequence[str]) -> list[Any]:
    if sort == "relevance" and terms:
        score = reduce(add, [_term_score(term) for term in terms])
        return [score.desc(), Skill.name.asc()]
    if sort == "updated":
        return [Skill.updated_at.desc(), Skill.name.asc()]
    if sort == "created":
        return [Skill.created_at.desc(), Skill.name.asc()]
    return [Skill.name.asc()]


def _filters(query: SkillQuery, terms: Sequence[str], tags: Sequence[str]) -> list[Any]:
    filters: list[Any] = []
    visibility = visibility_clause(query.viewer_id, query.viewer_is_admin)
    if visibility is not None:
        filters.append(visibility)
    if query.kind is not None:
        filters.append(Skill.kind == SkillKind(query.kind))
    category = query.category.strip().lower() if query.category else ""
    if category:
        filters.append(Skill.category == category)
    if query.owner_id is not None:
        filters.append(Skill.owner_id == query.owner_id)
    if query.statuses is not None:
        filters.append(Skill.status.in_([SkillStatus(status) for status in query.statuses]))
    filters.extend(_has_tag(tag) for tag in tags)
    filters.extend(_term_matches(term) for term in terms)
    return filters


async def search_skills(db: AsyncSession, query: SkillQuery) -> tuple[list[Skill], int]:
    terms = split_terms(query.q)
    tags = normalize_tags(query.tags)
    sort = _resolve_sort(query.sort, bool(terms))
    limit = max(query.limit, 0)
    offset = max(query.offset, 0)
    if len(tags) > MAX_TAG_FILTERS:
        return [], 0

    filters = _filters(query, terms, tags)
    matching = select(Skill.id).where(*filters).subquery()
    total = (await db.execute(select(func.count()).select_from(matching))).scalar_one()
    if total == 0 or limit == 0:
        return [], total

    statement = (
        select(Skill).where(*filters).order_by(*_ordering(sort, terms)).limit(limit).offset(offset)
    )
    items = list((await db.execute(statement)).scalars().all())
    return items, total


async def count_tags(
    db: AsyncSession,
    *,
    kind: SkillKind | None = None,
    viewer_id: str | None = None,
    viewer_is_admin: bool = False,
    limit: int = 50,
) -> list[tuple[str, int]]:
    total = func.count()
    statement = select(SkillTag.tag, total).join(Skill, Skill.id == SkillTag.skill_id)
    visibility = visibility_clause(viewer_id, viewer_is_admin)
    if visibility is not None:
        statement = statement.where(visibility)
    if kind is not None:
        statement = statement.where(Skill.kind == SkillKind(kind))
    statement = (
        statement.group_by(SkillTag.tag).order_by(total.desc(), SkillTag.tag).limit(max(limit, 0))
    )
    rows = (await db.execute(statement)).all()
    return [(tag, count) for tag, count in rows]


async def count_categories(
    db: AsyncSession,
    *,
    kind: SkillKind | None = None,
    viewer_id: str | None = None,
    viewer_is_admin: bool = False,
    limit: int = 50,
) -> list[tuple[str, int]]:
    total = func.count()
    statement = select(Skill.category, total).where(Skill.category.is_not(None))
    visibility = visibility_clause(viewer_id, viewer_is_admin)
    if visibility is not None:
        statement = statement.where(visibility)
    if kind is not None:
        statement = statement.where(Skill.kind == SkillKind(kind))
    statement = (
        statement.group_by(Skill.category)
        .order_by(total.desc(), Skill.category)
        .limit(max(limit, 0))
    )
    rows = (await db.execute(statement)).all()
    return [(category, count) for category, count in rows]


async def create_skill(
    db: AsyncSession,
    *,
    name: str,
    display_name: str,
    description: str,
    kind: SkillKind,
    owner_id: str,
    category: str | None = None,
    search_text: str | None = None,
    tags: Iterable[str] = (),
    status: SkillStatus = SkillStatus.DRAFT,
    latest_version: str | None = None,
) -> Skill:
    skill = Skill(
        name=name,
        display_name=display_name,
        description=description,
        kind=SkillKind(kind),
        owner_id=owner_id,
        category=category,
        search_text=search_text,
        status=SkillStatus(status),
        latest_version=latest_version,
        tag_rows=[SkillTag(tag=tag) for tag in normalize_tags(tags)],
    )
    db.add(skill)
    await db.flush()
    await db.refresh(skill)
    return skill


async def get_skill(db: AsyncSession, skill_id: str) -> Skill | None:
    result = await db.execute(select(Skill).where(Skill.id == skill_id))
    return result.scalar_one_or_none()


async def get_skill_by_name(db: AsyncSession, name: str) -> Skill | None:
    result = await db.execute(select(Skill).where(Skill.name == name))
    return result.scalar_one_or_none()


async def lock_skill(db: AsyncSession, skill_id: str) -> Skill | None:
    statement = (
        select(Skill)
        .where(Skill.id == skill_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return (await db.execute(statement)).scalar_one_or_none()


async def update_skill_denormalized(
    db: AsyncSession,
    skill: Skill,
    *,
    display_name: str,
    description: str,
    category: str | None,
    search_text: str | None,
    latest_version: str | None,
    tags: Iterable[str],
    status: SkillStatus | None = None,
) -> Skill:
    new_status = None if status is None else SkillStatus(status)
    skill.display_name = display_name
    skill.description = description
    skill.category = category
    skill.search_text = search_text
    skill.latest_version = latest_version
    if new_status is not None:
        skill.status = new_status
    skill.updated_at = func.now()

    wanted = normalize_tags(tags)
    current = {
        row.tag: row
        for row in (await db.execute(select(SkillTag).where(SkillTag.skill_id == skill.id)))
        .scalars()
        .all()
    }
    for tag, row in current.items():
        if tag not in wanted:
            await db.delete(row)
    db.add_all(SkillTag(skill_id=skill.id, tag=tag) for tag in wanted if tag not in current)

    await db.flush()
    await db.refresh(skill)
    return skill


async def set_skill_status(db: AsyncSession, skill: Skill, status: SkillStatus) -> Skill:
    skill.status = SkillStatus(status)
    await db.flush()
    await db.refresh(skill)
    return skill


async def create_version(
    db: AsyncSession,
    *,
    skill_id: str,
    version: str,
    blob_sha256: str,
    size_bytes: int,
    uncompressed_bytes: int,
    file_count: int,
    entrypoint: str,
    display_name: str,
    description: str,
    uploaded_by: str,
    root_prefix: str = "",
    category: str | None = None,
    tags: Iterable[str] = (),
    metadata: Mapping[str, Any] | None = None,
    manifest: Iterable[Mapping[str, Any]] = (),
    search_text: str = "",
    status: VersionStatus = VersionStatus.DRAFT,
    published_by: str | None = None,
    published_at: datetime | None = None,
) -> SkillVersion:
    ver = SkillVersion(
        skill_id=skill_id,
        version=version,
        status=VersionStatus(status),
        blob_sha256=blob_sha256,
        size_bytes=size_bytes,
        uncompressed_bytes=uncompressed_bytes,
        file_count=file_count,
        entrypoint=entrypoint,
        root_prefix=root_prefix,
        display_name=display_name,
        description=description,
        category=category,
        tags=json.dumps(list(tags), ensure_ascii=False),
        metadata_=json.dumps(dict(metadata or {}), ensure_ascii=False),
        manifest=json.dumps([dict(entry) for entry in manifest], ensure_ascii=False),
        search_text=search_text,
        uploaded_by=uploaded_by,
        published_by=published_by,
        published_at=published_at,
    )
    db.add(ver)
    await db.flush()
    await db.refresh(ver)
    return ver


async def get_version(db: AsyncSession, skill_id: str, version: str) -> SkillVersion | None:
    result = await db.execute(
        select(SkillVersion).where(
            SkillVersion.skill_id == skill_id,
            SkillVersion.version == version,
        )
    )
    return result.scalar_one_or_none()


async def get_version_by_id(db: AsyncSession, version_id: str) -> SkillVersion | None:
    result = await db.execute(select(SkillVersion).where(SkillVersion.id == version_id))
    return result.scalar_one_or_none()


async def list_versions(
    db: AsyncSession,
    skill_id: str,
    *,
    statuses: Collection[VersionStatus] | None = None,
) -> list[SkillVersion]:
    statement = (
        select(SkillVersion)
        .where(SkillVersion.skill_id == skill_id)
        .options(*(defer(column) for column in HEAVY_VERSION_COLUMNS))
    )
    if statuses is not None:
        statement = statement.where(
            SkillVersion.status.in_([VersionStatus(status) for status in statuses])
        )
    statement = statement.order_by(SkillVersion.created_at, SkillVersion.id).execution_options(
        populate_existing=True
    )
    return list((await db.execute(statement)).scalars().all())


async def get_version_search_text(db: AsyncSession, version_id: str) -> str:
    result = await db.execute(select(SkillVersion.search_text).where(SkillVersion.id == version_id))
    return result.scalar_one()


async def update_version(db: AsyncSession, version: SkillVersion, /, **fields: Any) -> SkillVersion:
    unknown = set(fields) - MUTABLE_VERSION_FIELDS
    if unknown:
        raise ValueError(f"Immutable version fields: {', '.join(sorted(unknown))}")
    if fields.get("status") is not None:
        fields["status"] = VersionStatus(fields["status"])
    for key, value in fields.items():
        setattr(version, key, value)
    await db.flush()
    await db.refresh(version)
    return version


async def count_versions_by_status(db: AsyncSession, skill_id: str) -> dict[VersionStatus, int]:
    rows = (
        await db.execute(
            select(SkillVersion.status, func.count())
            .where(SkillVersion.skill_id == skill_id)
            .group_by(SkillVersion.status)
        )
    ).all()
    counts = dict.fromkeys(VersionStatus, 0)
    for status, total in rows:
        counts[status] = total
    return counts
