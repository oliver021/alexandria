from __future__ import annotations

import pytest

from app.models import (
    Skill,
    SkillKind,
    SkillStatus,
    SkillVersion,
    TokenScope,
    UserRole,
    VersionStatus,
)
from app.services import access
from app.services.errors import ForbiddenError, UnauthorizedError
from app.services.principal import Principal

OWNER_ID = "owner-id"


def principal(
    user_id: str = "someone",
    *,
    role: UserRole = UserRole.PUBLISHER,
    scope: TokenScope = TokenScope.WRITE,
) -> Principal:
    return Principal(user_id=user_id, username=user_id, role=role, scope=scope)


ACTORS = {
    "anonymous": None,
    "stranger": principal("stranger"),
    "stranger_read": principal("stranger", scope=TokenScope.READ),
    "owner": principal(OWNER_ID),
    "owner_read": principal(OWNER_ID, scope=TokenScope.READ),
    "admin": principal("root", role=UserRole.ADMIN),
    "admin_read": principal("root", role=UserRole.ADMIN, scope=TokenScope.READ),
}


def make_skill(status: SkillStatus, latest_version: str | None) -> Skill:
    return Skill(
        name="sample",
        display_name="Sample",
        description="Sample",
        kind=SkillKind.SKILL,
        owner_id=OWNER_ID,
        status=status,
        latest_version=latest_version,
    )


def make_version(status: VersionStatus) -> SkillVersion:
    return SkillVersion(skill_id="s", version="1.0.0", status=status)


class TestRequireRead:
    @pytest.mark.parametrize("name", [name for name in ACTORS if name != "anonymous"])
    @pytest.mark.parametrize("public_read", [True, False])
    def test_any_principal_may_read_whatever_the_public_read_setting(self, name, public_read):
        access.require_read(ACTORS[name], public_read=public_read)

    def test_anonymous_may_read_when_the_registry_is_public(self):
        access.require_read(None, public_read=True)

    def test_anonymous_is_unauthorized_when_the_registry_is_private(self):
        with pytest.raises(UnauthorizedError):
            access.require_read(None, public_read=False)


class TestRequireWrite:
    def test_anonymous_is_unauthorized(self):
        with pytest.raises(UnauthorizedError):
            access.require_write(None)

    @pytest.mark.parametrize("name", ["stranger_read", "owner_read", "admin_read"])
    def test_read_scope_is_forbidden_even_for_admins(self, name):
        with pytest.raises(ForbiddenError):
            access.require_write(ACTORS[name])

    @pytest.mark.parametrize("name", ["stranger", "owner", "admin"])
    def test_write_scope_returns_the_same_principal(self, name):
        assert access.require_write(ACTORS[name]) is ACTORS[name]


class TestRequireAdmin:
    def test_anonymous_is_unauthorized(self):
        with pytest.raises(UnauthorizedError):
            access.require_admin(None)

    @pytest.mark.parametrize("name", ["stranger", "owner"])
    def test_a_publisher_with_write_scope_is_forbidden(self, name):
        with pytest.raises(ForbiddenError, match="admin"):
            access.require_admin(ACTORS[name])

    @pytest.mark.parametrize("name", ["stranger_read", "owner_read", "admin_read"])
    def test_read_scope_is_forbidden_even_for_admins(self, name):
        with pytest.raises(ForbiddenError, match="read-only"):
            access.require_admin(ACTORS[name])

    def test_an_admin_with_write_scope_returns_the_same_principal(self):
        assert access.require_admin(ACTORS["admin"]) is ACTORS["admin"]

    @pytest.mark.parametrize("name", list(ACTORS))
    def test_the_outcome_follows_the_role_and_the_scope(self, name):
        actor = ACTORS[name]
        if actor is None:
            expected = UnauthorizedError
        elif actor.is_admin and actor.can_write:
            expected = None
        else:
            expected = ForbiddenError
        if expected is None:
            assert access.require_admin(actor) is actor
            return
        with pytest.raises(expected) as raised:
            access.require_admin(actor)
        assert type(raised.value) is expected


class TestOwnerOrAdmin:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("anonymous", False),
            ("stranger", False),
            ("stranger_read", False),
            ("owner", True),
            ("owner_read", True),
            ("admin", True),
            ("admin_read", True),
        ],
    )
    def test_matrix(self, name, expected):
        assert access.is_owner_or_admin(ACTORS[name], OWNER_ID) is expected

    def test_a_lookalike_user_id_is_not_the_owner(self):
        assert not access.is_owner_or_admin(principal(OWNER_ID.upper()), OWNER_ID)
        assert not access.is_owner_or_admin(principal(OWNER_ID + " "), OWNER_ID)


class TestCanViewSkill:
    @pytest.mark.parametrize(
        ("status", "latest", "stranger_sees"),
        [
            (SkillStatus.PUBLISHED, "1.0.0", True),
            (SkillStatus.PUBLISHED, None, False),
            (SkillStatus.DRAFT, None, False),
            (SkillStatus.DRAFT, "1.0.0", False),
            (SkillStatus.UNPUBLISHED, "1.0.0", False),
            (SkillStatus.UNPUBLISHED, None, False),
        ],
    )
    @pytest.mark.parametrize("name", list(ACTORS))
    def test_matrix(self, name, status, latest, stranger_sees):
        skill = make_skill(status, latest)
        privileged = name.startswith(("owner", "admin"))
        expected = True if privileged else stranger_sees
        assert access.can_view_skill(ACTORS[name], skill) is expected


class TestCanViewVersion:
    @pytest.mark.parametrize(
        ("status", "stranger_sees"),
        [
            (VersionStatus.PUBLISHED, True),
            (VersionStatus.DEPRECATED, True),
            (VersionStatus.DRAFT, False),
        ],
    )
    @pytest.mark.parametrize("name", list(ACTORS))
    def test_matrix(self, name, status, stranger_sees):
        skill = make_skill(SkillStatus.PUBLISHED, "1.0.0")
        privileged = name.startswith(("owner", "admin"))
        expected = True if privileged else stranger_sees
        assert access.can_view_version(ACTORS[name], skill, make_version(status)) is expected


class TestRequireManage:
    def test_anonymous_is_unauthorized(self):
        with pytest.raises(UnauthorizedError):
            access.require_manage(None, make_skill(SkillStatus.PUBLISHED, "1.0.0"))

    @pytest.mark.parametrize("name", ["stranger_read", "owner_read", "admin_read"])
    def test_read_scope_is_forbidden_before_ownership_is_considered(self, name):
        with pytest.raises(ForbiddenError, match="read-only"):
            access.require_manage(ACTORS[name], make_skill(SkillStatus.PUBLISHED, "1.0.0"))

    def test_a_writer_that_is_not_the_owner_is_forbidden(self):
        with pytest.raises(ForbiddenError, match="owner"):
            access.require_manage(ACTORS["stranger"], make_skill(SkillStatus.PUBLISHED, "1.0.0"))

    @pytest.mark.parametrize("name", ["owner", "admin"])
    def test_owner_and_admin_writers_are_allowed(self, name):
        skill = make_skill(SkillStatus.UNPUBLISHED, None)
        assert access.require_manage(ACTORS[name], skill) is ACTORS[name]
