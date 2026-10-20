from __future__ import annotations

from dataclasses import dataclass

from app.models.token import TokenScope
from app.models.user import UserRole


@dataclass(frozen=True)
class Principal:
    user_id: str
    username: str
    role: UserRole
    scope: TokenScope = TokenScope.WRITE

    @property
    def is_admin(self) -> bool:
        return self.role == UserRole.ADMIN

    @property
    def can_write(self) -> bool:
        return self.scope == TokenScope.WRITE
