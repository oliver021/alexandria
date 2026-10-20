from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import ColumnElement, and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.token import ApiToken, TokenScope


def _active_clause(now: datetime) -> ColumnElement[bool]:
    return and_(
        ApiToken.revoked_at.is_(None),
        or_(ApiToken.expires_at.is_(None), ApiToken.expires_at > now),
    )


async def create_token(
    db: AsyncSession,
    *,
    user_id: str,
    name: str,
    prefix: str,
    token_hash: str,
    scope: TokenScope,
    expires_at: datetime | None = None,
) -> ApiToken:
    token = ApiToken(
        user_id=user_id,
        name=name,
        prefix=prefix,
        token_hash=token_hash,
        scope=TokenScope(scope),
        expires_at=expires_at,
    )
    db.add(token)
    await db.flush()
    await db.refresh(token)
    return token


async def get_token(db: AsyncSession, token_id: str) -> ApiToken | None:
    result = await db.execute(select(ApiToken).where(ApiToken.id == token_id))
    return result.scalar_one_or_none()


async def get_token_by_hash(db: AsyncSession, token_hash: str) -> ApiToken | None:
    result = await db.execute(select(ApiToken).where(ApiToken.token_hash == token_hash))
    return result.scalar_one_or_none()


async def get_active_token_by_hash(
    db: AsyncSession, token_hash: str, *, now: datetime | None = None
) -> ApiToken | None:
    statement = select(ApiToken).where(
        ApiToken.token_hash == token_hash, _active_clause(now or datetime.now(UTC))
    )
    return (await db.execute(statement)).scalar_one_or_none()


async def list_tokens(
    db: AsyncSession,
    user_id: str,
    *,
    active_only: bool = False,
    now: datetime | None = None,
) -> list[ApiToken]:
    statement = select(ApiToken).where(ApiToken.user_id == user_id)
    if active_only:
        statement = statement.where(_active_clause(now or datetime.now(UTC)))
    statement = statement.order_by(ApiToken.created_at.desc(), ApiToken.id)
    return list((await db.execute(statement)).scalars().all())


async def count_active_tokens(
    db: AsyncSession, user_id: str, *, now: datetime | None = None
) -> int:
    statement = (
        select(func.count())
        .select_from(ApiToken)
        .where(ApiToken.user_id == user_id, _active_clause(now or datetime.now(UTC)))
    )
    return (await db.execute(statement)).scalar_one()


async def revoke_token(
    db: AsyncSession, token: ApiToken, *, now: datetime | None = None
) -> ApiToken:
    if token.revoked_at is None:
        token.revoked_at = now or datetime.now(UTC)
        await db.flush()
        await db.refresh(token)
    return token


async def touch_last_used(
    db: AsyncSession,
    token: ApiToken,
    *,
    now: datetime | None = None,
    min_interval: timedelta | None = None,
) -> bool:
    moment = now or datetime.now(UTC)
    statement = update(ApiToken).where(ApiToken.id == token.id).values(last_used_at=moment)
    if min_interval is not None:
        statement = statement.where(
            or_(ApiToken.last_used_at.is_(None), ApiToken.last_used_at <= moment - min_interval)
        )
    result = await db.execute(statement.execution_options(synchronize_session=False))
    if result.rowcount == 0:
        return False
    await db.refresh(token)
    return True
