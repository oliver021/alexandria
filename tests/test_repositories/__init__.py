from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.session import configure_sqlite_engine
from app.models import Base, User, UserRole


async def create_test_engine() -> AsyncEngine:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    configure_sqlite_engine(engine)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine


async def make_user(
    db: AsyncSession, username: str, role: UserRole = UserRole.PUBLISHER, is_active: bool = True
) -> User:
    user = User(
        username=username,
        email=f"{username}@example.com",
        password_hash="not-a-real-hash",
        role=role,
        is_active=is_active,
    )
    db.add(user)
    await db.flush()
    return user
