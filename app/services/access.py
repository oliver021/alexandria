from __future__ import annotations

from app.models.skill import Skill, SkillStatus, SkillVersion, VersionStatus
from app.services.errors import ForbiddenError, UnauthorizedError
from app.services.principal import Principal

PUBLIC_VERSION_STATUSES: frozenset[VersionStatus] = frozenset(
    {VersionStatus.PUBLISHED, VersionStatus.DEPRECATED}
)


def require_read(principal: Principal | None, *, public_read: bool) -> None:
    if principal is None and not public_read:
        raise UnauthorizedError("Authentication is required to read this registry")


def require_write(principal: Principal | None) -> Principal:
    if principal is None:
        raise UnauthorizedError()
    if not principal.can_write:
        raise ForbiddenError("This credential has read-only scope")
    return principal


def require_admin(principal: Principal | None) -> Principal:
    admin = require_write(principal)
    if not admin.is_admin:
        raise ForbiddenError("Only an admin can manage users")
    return admin


def is_owner_or_admin(principal: Principal | None, owner_id: str) -> bool:
    if principal is None:
        return False
    return principal.is_admin or principal.user_id == owner_id


def can_view_skill(principal: Principal | None, skill: Skill) -> bool:
    if is_owner_or_admin(principal, skill.owner_id):
        return True
    return skill.status == SkillStatus.PUBLISHED and skill.latest_version is not None


def can_view_version(principal: Principal | None, skill: Skill, version: SkillVersion) -> bool:
    if is_owner_or_admin(principal, skill.owner_id):
        return True
    return version.status in PUBLIC_VERSION_STATUSES


def require_manage(principal: Principal | None, skill: Skill) -> Principal:
    writer = require_write(principal)
    if not is_owner_or_admin(writer, skill.owner_id):
        raise ForbiddenError("Only the owner of this entry or an admin can change it")
    return writer
