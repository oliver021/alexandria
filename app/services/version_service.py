from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.logging import get_logger
from app.models.schemas import VersionDetail, VersionSummary
from app.models.skill import VersionStatus
from app.repositories import skill_repo
from app.services import access, catalog, mappers
from app.services.errors import ConflictError
from app.services.principal import Principal
from app.services.transactions import write_transaction

logger = get_logger("app.services.version")


class VersionService:
    def __init__(
        self,
        db: AsyncSession,
        *,
        public_read: bool | None = None,
        public_url: str | None = None,
    ) -> None:
        self.db = db
        self.public_read = settings.registry.public_read if public_read is None else public_read
        base_url = settings.server.public_url if public_url is None else public_url
        self.public_url = base_url.rstrip("/")

    async def list(self, principal: Principal | None, ref: str) -> list[VersionSummary]:
        access.require_read(principal, public_read=self.public_read)
        skill = await catalog.get_visible_skill(self.db, principal, ref)
        versions = await catalog.list_visible_versions(self.db, principal, skill)
        return [mappers.version_summary(item) for item in versions]

    async def get(
        self, principal: Principal | None, ref: str, version: str = catalog.LATEST
    ) -> VersionDetail:
        access.require_read(principal, public_read=self.public_read)
        skill = await catalog.get_visible_skill(self.db, principal, ref)
        target = await catalog.resolve_version(self.db, principal, skill, version)
        url = mappers.download_url(self.public_url, skill.name, target.version)
        return mappers.version_detail(target, url=url)

    async def publish(self, principal: Principal | None, ref: str, version: str) -> VersionSummary:
        skill, writer = await catalog.get_manageable_skill(self.db, principal, ref)
        target = await catalog.get_exact_version(self.db, writer, skill, version)
        if target.status == VersionStatus.PUBLISHED:
            return mappers.version_summary(target)
        skill_id, name, previous = skill.id, skill.name, VersionStatus(target.status).value
        fields: dict[str, Any] = {"status": VersionStatus.PUBLISHED, "deprecated_at": None}
        if target.published_at is None:
            fields["published_by"] = writer.user_id
            fields["published_at"] = datetime.now(UTC)
        async with write_transaction(self.db):
            await skill_repo.lock_skill(self.db, skill_id)
            await skill_repo.update_version(self.db, target, **fields)
            await catalog.refresh_skill(self.db, skill)
            summary = mappers.version_summary(target)
        logger.info(
            f"Version published: {name} {summary.version}",
            extra={
                "extra_data": {
                    "skill_id": summary.skill_id,
                    "version": summary.version,
                    "from_status": previous,
                    "user_id": writer.user_id,
                }
            },
        )
        return summary

    async def deprecate(
        self, principal: Principal | None, ref: str, version: str
    ) -> VersionSummary:
        skill, writer = await catalog.get_manageable_skill(self.db, principal, ref)
        target = await catalog.get_exact_version(self.db, writer, skill, version)
        if target.status == VersionStatus.DEPRECATED:
            return mappers.version_summary(target)
        skill_id, name, number = skill.id, skill.name, target.version
        if target.status != VersionStatus.PUBLISHED:
            self._reject("only published versions can be deprecated", skill_id, number, writer)
            raise ConflictError("Only a published version can be deprecated")
        async with write_transaction(self.db):
            await skill_repo.lock_skill(self.db, skill_id)
            await skill_repo.update_version(
                self.db, target, status=VersionStatus.DEPRECATED, deprecated_at=datetime.now(UTC)
            )
            remaining = await skill_repo.list_versions(
                self.db, skill_id, statuses=(VersionStatus.PUBLISHED,)
            )
            if not remaining:
                self._reject("it is the only published version", skill_id, number, writer)
                raise ConflictError(
                    "The only published version cannot be deprecated; hide the entry instead"
                )
            await catalog.refresh_skill(self.db, skill)
            summary = mappers.version_summary(target)
        logger.info(
            f"Version deprecated: {name} {summary.version}",
            extra={
                "extra_data": {
                    "skill_id": summary.skill_id,
                    "version": summary.version,
                    "user_id": writer.user_id,
                }
            },
        )
        return summary

    @staticmethod
    def _reject(reason: str, skill_id: str, version: str, writer: Principal) -> None:
        logger.warning(
            f"Deprecation rejected: {reason}",
            extra={
                "extra_data": {
                    "skill_id": skill_id,
                    "version": version,
                    "user_id": writer.user_id,
                }
            },
        )
