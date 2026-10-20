from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.logging import get_logger
from app.models.schemas import PaginatedResult, SkillDetail, SkillSummary, TaxonomyItem
from app.models.skill import SkillStatus
from app.repositories import skill_repo
from app.repositories.skill_repo import SkillQuery
from app.services import access, catalog, mappers
from app.services.errors import UnauthorizedError
from app.services.principal import Principal
from app.services.transactions import write_transaction

logger = get_logger("app.services.skill")

SkillPage = PaginatedResult[SkillSummary]


class SkillService:
    def __init__(self, db: AsyncSession, *, public_read: bool | None = None) -> None:
        self.db = db
        self.public_read = settings.registry.public_read if public_read is None else public_read

    async def search(
        self,
        principal: Principal | None,
        *,
        q: str | None = None,
        kind: str | None = None,
        tags: Iterable[str] = (),
        category: str | None = None,
        status: str | None = None,
        mine: bool = False,
        sort: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> SkillPage:
        access.require_read(principal, public_read=self.public_read)
        kind_filter = catalog.parse_kind(kind)
        status_filter = catalog.parse_skill_status(status)
        sort_value = catalog.check_sort(sort)
        page_limit = catalog.check_limit(limit)
        page_offset = catalog.check_offset(offset)
        text = catalog.clean_query(q)
        category_filter = catalog.clean_category(category)
        tag_filters = catalog.clean_tags(tags)
        if mine and principal is None:
            raise UnauthorizedError("Authentication is required to list your own entries")
        viewer_id, viewer_is_admin = catalog.viewer(principal)
        query = SkillQuery(
            q=text,
            kind=kind_filter,
            tags=tag_filters,
            category=category_filter,
            owner_id=viewer_id if mine else None,
            statuses=None if status_filter is None else (status_filter,),
            viewer_id=viewer_id,
            viewer_is_admin=viewer_is_admin,
            sort=sort_value,
            limit=page_limit,
            offset=page_offset,
        )
        skills, total = await skill_repo.search_skills(self.db, query)
        return SkillPage(
            items=[mappers.skill_summary(skill) for skill in skills],
            total=total,
            limit=page_limit,
            offset=page_offset,
        )

    async def get(self, principal: Principal | None, ref: str) -> SkillDetail:
        access.require_read(principal, public_read=self.public_read)
        skill = await catalog.get_visible_skill(self.db, principal, ref)
        versions = await catalog.list_visible_versions(self.db, principal, skill)
        return mappers.skill_detail(skill, versions)

    async def hide(self, principal: Principal | None, ref: str) -> None:
        skill, writer = await catalog.get_manageable_skill(self.db, principal, ref)
        if skill.status == SkillStatus.UNPUBLISHED:
            return
        skill_id, name = skill.id, skill.name
        async with write_transaction(self.db):
            await skill_repo.set_skill_status(self.db, skill, SkillStatus.UNPUBLISHED)
        logger.info(
            f"Skill hidden: {name}",
            extra={"extra_data": {"skill_id": skill_id, "user_id": writer.user_id}},
        )

    async def restore(self, principal: Principal | None, ref: str) -> SkillDetail:
        skill, writer = await catalog.get_manageable_skill(self.db, principal, ref)
        async with write_transaction(self.db):
            await skill_repo.lock_skill(self.db, skill.id)
            await catalog.refresh_skill(self.db, skill, reopen=True)
            versions = await catalog.list_visible_versions(self.db, writer, skill)
            detail = mappers.skill_detail(skill, versions)
        logger.info(
            f"Skill restored: {detail.name}",
            extra={
                "extra_data": {
                    "skill_id": detail.id,
                    "status": detail.status,
                    "user_id": writer.user_id,
                }
            },
        )
        return detail

    async def tags(
        self, principal: Principal | None, *, kind: str | None = None, limit: int = 50
    ) -> list[TaxonomyItem]:
        access.require_read(principal, public_read=self.public_read)
        kind_filter = catalog.parse_kind(kind)
        size = catalog.check_limit(limit, maximum=catalog.MAX_TAXONOMY_SIZE)
        viewer_id, viewer_is_admin = catalog.viewer(principal)
        rows = await skill_repo.count_tags(
            self.db,
            kind=kind_filter,
            viewer_id=viewer_id,
            viewer_is_admin=viewer_is_admin,
            limit=size,
        )
        return [TaxonomyItem(name=name, count=count) for name, count in rows]

    async def categories(
        self, principal: Principal | None, *, kind: str | None = None, limit: int = 50
    ) -> list[TaxonomyItem]:
        access.require_read(principal, public_read=self.public_read)
        kind_filter = catalog.parse_kind(kind)
        size = catalog.check_limit(limit, maximum=catalog.MAX_TAXONOMY_SIZE)
        viewer_id, viewer_is_admin = catalog.viewer(principal)
        rows = await skill_repo.count_categories(
            self.db,
            kind=kind_filter,
            viewer_id=viewer_id,
            viewer_is_admin=viewer_is_admin,
            limit=size,
        )
        return [TaxonomyItem(name=name, count=count) for name, count in rows]
