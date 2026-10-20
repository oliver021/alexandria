from __future__ import annotations

import asyncio
import secrets
from datetime import UTC, datetime, timedelta
from functools import lru_cache

from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.password import MAX_PASSWORD_LENGTH, hash_password, verify_password
from app.config.settings import settings
from app.logging import get_logger
from app.models.user import User
from app.text import is_valid_text

logger = get_logger("app.auth.local")

JWT_OPTIONS = {"require_exp": True, "require_sub": True}


def is_valid_password(value: object) -> bool:
    return isinstance(value, str) and len(value) <= MAX_PASSWORD_LENGTH and is_valid_text(value)


@lru_cache(maxsize=1)
def _decoy_password_hash() -> str:
    return hash_password(secrets.token_urlsafe(32))


_decoy_password_hash()


class LocalAuthProvider:
    def __init__(
        self,
        *,
        secret: str | None = None,
        algorithm: str | None = None,
        expiry_minutes: int | None = None,
    ) -> None:
        self.secret = settings.auth.jwt_secret if secret is None else secret
        self.algorithm = settings.auth.jwt_algorithm if algorithm is None else algorithm
        self.expiry_minutes = (
            settings.auth.jwt_expiry_minutes if expiry_minutes is None else expiry_minutes
        )
        self._decoy_hash = _decoy_password_hash()

    async def authenticate(self, db: AsyncSession, username: str, password: str) -> User | None:
        if not is_valid_text(username) or not is_valid_password(password):
            return None
        result = await db.execute(select(User).where(User.username == username))
        user = result.scalar_one_or_none()
        stored_hash = self._decoy_hash if user is None else user.password_hash
        try:
            matches = await asyncio.to_thread(verify_password, password, stored_hash)
        except ValueError:
            if user is not None:
                logger.warning(
                    "Stored password hash is not usable",
                    extra={"extra_data": {"user_id": user.id}},
                )
            return None
        if user is None or not matches or not user.is_active:
            return None
        return user

    async def create_token(self, user: User) -> str:
        expire = datetime.now(UTC) + timedelta(minutes=self.expiry_minutes)
        payload = {"sub": user.id, "role": user.role.value, "exp": expire}
        return jwt.encode(payload, self.secret, algorithm=self.algorithm)

    def _subject(self, token: str) -> str | None:
        if not isinstance(token, str) or not token.isascii():
            return None
        try:
            payload = jwt.decode(
                token, self.secret, algorithms=[self.algorithm], options=JWT_OPTIONS
            )
        except (JWTError, ValueError, ArithmeticError):
            return None
        subject = payload.get("sub")
        if not isinstance(subject, str) or not subject:
            return None
        return subject

    async def get_user_from_token(self, db: AsyncSession, token: str) -> User | None:
        user_id = self._subject(token)
        if user_id is None:
            return None
        result = await db.execute(select(User).where(User.id == user_id))
        return result.scalar_one_or_none()

    async def get_active_user_from_token(self, db: AsyncSession, token: str) -> User | None:
        user = await self.get_user_from_token(db, token)
        if user is None or not user.is_active:
            return None
        return user
