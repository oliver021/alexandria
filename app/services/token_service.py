from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, TypeGuard

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.logging import get_logger
from app.models.schemas import TokenCreate, TokenCreated, TokenResponse
from app.models.token import ApiToken, TokenScope
from app.repositories import token_repo, user_repo
from app.services.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
    ValidationError,
)
from app.services.principal import Principal

logger = get_logger("app.services.token")

TOKEN_PREFIX = "alx_"
TOKEN_ENTROPY_BYTES = 32
DISPLAY_PREFIX_LENGTH = 12
MAX_TOKEN_LENGTH = 256
LAST_USED_INTERVAL = timedelta(minutes=10)

_TOKEN_SHAPE = re.compile(re.escape(TOKEN_PREFIX) + r"[A-Za-z0-9_-]+")
_TOKEN_ID_SHAPE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

Clock = Callable[[], datetime]


def hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _system_clock() -> datetime:
    return datetime.now(UTC)


def _to_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _is_well_formed(value: object) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and len(value) <= MAX_TOKEN_LENGTH
        and _TOKEN_SHAPE.fullmatch(value) is not None
    )


def _is_token_id(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and _TOKEN_ID_SHAPE.fullmatch(value) is not None


def _fields(token: ApiToken) -> dict[str, Any]:
    return {
        "id": token.id,
        "name": token.name,
        "prefix": token.prefix,
        "scope": token.scope.value,
        "created_at": token.created_at,
        "expires_at": token.expires_at,
        "last_used_at": token.last_used_at,
        "revoked_at": token.revoked_at,
    }


class TokenService:
    def __init__(
        self,
        db: AsyncSession,
        *,
        max_per_user: int | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.db = db
        self.max_per_user = settings.tokens.max_per_user if max_per_user is None else max_per_user
        self._clock = clock or _system_clock

    def _now(self) -> datetime:
        return _to_utc(self._clock())

    @staticmethod
    def _require_write(principal: Principal | None) -> Principal:
        if principal is None:
            raise UnauthorizedError("Authentication required")
        if not principal.can_write:
            raise ForbiddenError("A write-scoped credential is required to manage API tokens")
        return principal

    async def create(self, principal: Principal | None, data: TokenCreate) -> TokenCreated:
        principal = self._require_write(principal)
        name = data.name.strip()
        if not name:
            raise ValidationError("The token name must not be blank")
        user = await user_repo.get_user(self.db, principal.user_id)
        if user is None:
            raise UnauthorizedError("The account is not active")
        raw_token = TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_ENTROPY_BYTES)
        try:
            await self.db.refresh(user, with_for_update=True)
            if not user.is_active:
                raise UnauthorizedError("The account is not active")
            now = self._now()
            if await self._count_active(principal.user_id, now) >= self.max_per_user:
                raise self._limit_reached()
            expires_at = None
            if data.expires_in_days is not None:
                expires_at = now + timedelta(days=data.expires_in_days)
            token = await token_repo.create_token(
                self.db,
                user_id=principal.user_id,
                name=name,
                prefix=raw_token[:DISPLAY_PREFIX_LENGTH],
                token_hash=hash_token(raw_token),
                scope=TokenScope(data.scope),
                expires_at=expires_at,
            )
            if await self._count_active(principal.user_id, now) > self.max_per_user:
                raise self._limit_reached()
            created = TokenCreated(**_fields(token), token=raw_token)
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            raise
        logger.info(
            "API token created",
            extra={
                "extra_data": {
                    "token_id": created.id,
                    "user_id": principal.user_id,
                    "scope": created.scope,
                }
            },
        )
        return created

    async def _count_active(self, user_id: str, now: datetime) -> int:
        return await token_repo.count_active_tokens(self.db, user_id, now=now)

    def _limit_reached(self) -> ConflictError:
        return ConflictError(
            f"The limit of {self.max_per_user} active API tokens per user has been reached"
        )

    async def list(self, principal: Principal | None) -> list[TokenResponse]:
        principal = self._require_write(principal)
        tokens = await token_repo.list_tokens(self.db, principal.user_id)
        return [TokenResponse(**_fields(token)) for token in tokens]

    async def revoke(self, principal: Principal | None, token_id: str) -> None:
        principal = self._require_write(principal)
        token = await token_repo.get_token(self.db, token_id) if _is_token_id(token_id) else None
        if token is None or (token.user_id != principal.user_id and not principal.is_admin):
            raise NotFoundError("Token not found")
        if token.revoked_at is not None:
            return
        owner_id = token.user_id
        try:
            await token_repo.revoke_token(self.db, token, now=self._now())
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            raise
        logger.info(
            "API token revoked",
            extra={
                "extra_data": {
                    "token_id": token_id,
                    "user_id": owner_id,
                    "revoked_by": principal.user_id,
                }
            },
        )

    async def verify(self, raw_token: str | None) -> Principal | None:
        if not _is_well_formed(raw_token):
            return None
        now = self._now()
        token = await token_repo.get_active_token_by_hash(self.db, hash_token(raw_token), now=now)
        if token is None:
            return None
        user = await user_repo.get_user(self.db, token.user_id)
        if user is None or not user.is_active:
            logger.warning(
                "API token rejected because its owner is unavailable",
                extra={"extra_data": {"token_id": token.id, "user_id": token.user_id}},
            )
            return None
        principal = Principal(
            user_id=user.id, username=user.username, role=user.role, scope=token.scope
        )
        await self._record_use(token, now)
        return principal

    async def _record_use(self, token: ApiToken, now: datetime) -> None:
        token_id = token.id
        try:
            touched = await token_repo.touch_last_used(
                self.db, token, now=now, min_interval=LAST_USED_INTERVAL
            )
            if touched:
                await self.db.commit()
        except SQLAlchemyError:
            await self.db.rollback()
            logger.warning(
                "Could not record the use of an API token",
                extra={"extra_data": {"token_id": token_id}},
            )
