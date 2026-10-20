from __future__ import annotations

from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User


class AuthProvider(Protocol):
    async def authenticate(self, db: AsyncSession, username: str, password: str) -> User | None: ...

    async def create_token(self, user: User) -> str: ...

    async def get_user_from_token(self, db: AsyncSession, token: str) -> User | None: ...

    async def get_active_user_from_token(self, db: AsyncSession, token: str) -> User | None: ...
