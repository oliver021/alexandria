from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models import SkillKind, SkillStatus, TokenScope, User, UserRole, VersionStatus
from app.models.schemas import UploadResult
from app.repositories import skill_repo
from app.services.errors import DomainError, ForbiddenError, NotFoundError, UnauthorizedError
from app.services.package_service import PackageService
from app.services.principal import Principal
from tests.helpers.packages import make_grounding_zip, make_skill_zip

ACTOR_NAMES = (
    "anonymous",
    "stranger",
    "stranger_read",
    "owner",
    "owner_read",
    "admin",
    "admin_read",
)


ENTRY_OWNERS = {
    "pub-skill": "owner",
    "draft-skill": "owner",
    "hidden-ground": "owner",
    "foreign-skill": "stranger",
}

PRIVILEGED_ACTORS = frozenset({"owner", "owner_read", "admin", "admin_read"})

VISIBLE_TO = {
    "pub-skill": frozenset(ACTOR_NAMES),
    "foreign-skill": frozenset(ACTOR_NAMES),
    "draft-skill": PRIVILEGED_ACTORS,
    "hidden-ground": PRIVILEGED_ACTORS,
}

MANAGERS = {
    "pub-skill": frozenset({"owner", "admin"}),
    "draft-skill": frozenset({"owner", "admin"}),
    "hidden-ground": frozenset({"owner", "admin"}),
    "foreign-skill": frozenset({"stranger", "admin"}),
}

SEEDED_VERSIONS = {
    "pub-skill": "1.0.0",
    "draft-skill": "0.1.0",
    "hidden-ground": "1.0.0",
    "foreign-skill": "1.0.0",
}


def write_outcome(actor: str, entry: str) -> type[DomainError] | None:
    if actor == "anonymous":
        return UnauthorizedError
    if actor.endswith("_read"):
        return ForbiddenError
    if actor not in VISIBLE_TO[entry]:
        return NotFoundError
    if actor in MANAGERS[entry]:
        return None
    return ForbiddenError


@dataclass(frozen=True)
class Actors:
    owner: Principal
    owner_read: Principal
    stranger: Principal
    stranger_read: Principal
    admin: Principal
    admin_read: Principal
    anonymous: None = None

    def get(self, name: str) -> Principal | None:
        return getattr(self, name)


@dataclass(frozen=True)
class Catalog:
    published: str
    published_id: str
    drafty: str
    hidden: str
    foreign: str


async def make_user(
    db: AsyncSession,
    username: str,
    *,
    role: UserRole = UserRole.PUBLISHER,
    is_active: bool = True,
) -> User:
    user = User(
        username=username,
        email=f"{username}@example.com",
        password_hash="not-a-real-hash",
        role=role,
        is_active=is_active,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


def principal_of(user: User, *, scope: TokenScope = TokenScope.WRITE) -> Principal:
    return Principal(user_id=user.id, username=user.username, role=user.role, scope=scope)


async def make_principal(
    db: AsyncSession,
    username: str,
    *,
    role: UserRole = UserRole.PUBLISHER,
    scope: TokenScope = TokenScope.WRITE,
) -> Principal:
    return principal_of(await make_user(db, username, role=role), scope=scope)


async def make_actors(db: AsyncSession) -> Actors:
    owner = await make_user(db, "alice")
    owner_write, owner_read = principal_of(owner), principal_of(owner, scope=TokenScope.READ)
    stranger = await make_user(db, "bob")
    stranger_write = principal_of(stranger)
    stranger_read = principal_of(stranger, scope=TokenScope.READ)
    admin = await make_user(db, "root", role=UserRole.ADMIN)
    return Actors(
        owner=owner_write,
        owner_read=owner_read,
        stranger=stranger_write,
        stranger_read=stranger_read,
        admin=principal_of(admin),
        admin_read=principal_of(admin, scope=TokenScope.READ),
    )


@contextmanager
def record_writes(engine: AsyncEngine) -> Iterator[list[str]]:
    statements: list[str] = []

    def record(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().split(None, 1)[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
            statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)


def _user_id(actor: Principal | str) -> str:
    return actor if isinstance(actor, str) else actor.user_id


async def add_skill(
    db: AsyncSession,
    owner: Principal | str,
    name: str,
    *,
    kind: SkillKind = SkillKind.SKILL,
    status: SkillStatus = SkillStatus.PUBLISHED,
    latest_version: str | None = "1.0.0",
    display_name: str | None = None,
    description: str | None = None,
    category: str | None = None,
    tags: tuple[str, ...] | list[str] = (),
    search_text: str | None = None,
) -> str:
    skill = await skill_repo.create_skill(
        db,
        name=name,
        display_name=display_name or name.replace("-", " ").title(),
        description=description if description is not None else f"Description of {name}",
        kind=kind,
        owner_id=_user_id(owner),
        category=category,
        search_text=search_text,
        tags=tags,
        status=status,
        latest_version=latest_version,
    )
    await db.commit()
    return skill.id


async def add_version(
    db: AsyncSession,
    skill_id: str,
    version: str,
    uploader: Principal | str,
    *,
    status: VersionStatus = VersionStatus.PUBLISHED,
    sha256: str = "a" * 64,
    display_name: str = "Seeded",
    description: str = "Seeded version",
    category: str | None = None,
    tags: tuple[str, ...] | list[str] = (),
    search_text: str = "",
    **overrides: Any,
) -> str:
    uploader_id = _user_id(uploader)
    published = status in (VersionStatus.PUBLISHED, VersionStatus.DEPRECATED)
    values: dict[str, Any] = {
        "skill_id": skill_id,
        "version": version,
        "blob_sha256": sha256,
        "size_bytes": 100,
        "uncompressed_bytes": 200,
        "file_count": 1,
        "entrypoint": "SKILL.md",
        "display_name": display_name,
        "description": description,
        "category": category,
        "tags": list(tags),
        "search_text": search_text,
        "uploaded_by": uploader_id,
        "status": status,
        "published_by": uploader_id if published else None,
        "published_at": datetime.now(UTC) if published else None,
    }
    values.update(overrides)
    created = await skill_repo.create_version(db, **values)
    if status == VersionStatus.DEPRECATED:
        await skill_repo.update_version(db, created, deprecated_at=datetime.now(UTC))
    await db.commit()
    return created.id


async def seed_catalog(db: AsyncSession, actors: Actors) -> Catalog:
    owner, stranger = actors.owner, actors.stranger
    published_id = await add_skill(
        db,
        owner,
        "pub-skill",
        latest_version="1.1.0",
        description="Published skill",
        category="ops",
        tags=("alpha", "beta"),
    )
    for number, status in (
        ("0.9.0", VersionStatus.DEPRECATED),
        ("1.0.0", VersionStatus.PUBLISHED),
        ("1.1.0", VersionStatus.PUBLISHED),
        ("2.0.0-beta.1", VersionStatus.PUBLISHED),
        ("2.0.0", VersionStatus.DRAFT),
    ):
        await add_version(db, published_id, number, owner, status=status, tags=("alpha", "beta"))

    drafty_id = await add_skill(
        db, owner, "draft-skill", status=SkillStatus.DRAFT, latest_version=None
    )
    await add_version(db, drafty_id, "0.1.0", owner, status=VersionStatus.DRAFT)

    hidden_id = await add_skill(
        db,
        owner,
        "hidden-ground",
        kind=SkillKind.GROUNDING,
        status=SkillStatus.UNPUBLISHED,
        latest_version="1.0.0",
    )
    await add_version(db, hidden_id, "1.0.0", owner, status=VersionStatus.PUBLISHED)

    foreign_id = await add_skill(db, stranger, "foreign-skill")
    await add_version(db, foreign_id, "1.0.0", stranger, status=VersionStatus.PUBLISHED)
    return Catalog(
        published="pub-skill",
        published_id=published_id,
        drafty="draft-skill",
        hidden="hidden-ground",
        foreign="foreign-skill",
    )


async def upload_package(
    service: PackageService,
    principal: Principal | None,
    name: str = "sample-skill",
    version: str | None = "1.0.0",
    *,
    kind: str = "skill",
    publish: bool | None = True,
    **options: Any,
) -> UploadResult:
    maker = make_grounding_zip if kind == "grounding" else make_skill_zip
    return await service.upload(principal, maker(name, version, **options), publish=publish)
