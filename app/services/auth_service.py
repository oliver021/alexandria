from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.local import LocalAuthProvider
from app.auth.provider import AuthProvider
from app.logging import get_logger
from app.models.schemas import UserCreate, UserResponse
from app.models.token import TokenScope
from app.repositories import user_repo
from app.services.errors import UnauthorizedError
from app.services.principal import Principal
from app.services.token_service import TOKEN_PREFIX, TokenService
from app.services.user_service import register_user

logger = get_logger("app.services.auth")


class AuthService:
    def __init__(
        self,
        db: AsyncSession,
        *,
        auth_provider: AuthProvider | None = None,
        token_service: TokenService | None = None,
    ) -> None:
        self.db = db
        self.auth_provider = auth_provider or LocalAuthProvider()
        self.token_service = token_service or TokenService(db)

    async def create_user(self, data: UserCreate) -> UserResponse:
        return await register_user(self.db, data)

    async def authenticate(self, username: str, password: str) -> tuple[UserResponse, str]:
        user = await self.auth_provider.authenticate(self.db, username, password)
        if not user:
            logger.warning(f"Failed login attempt for: {username}")
            raise UnauthorizedError("Invalid credentials")
        token = await self.auth_provider.create_token(user)
        account = UserResponse.model_validate(user)
        logger.info(
            f"User authenticated: {username}",
            extra={"extra_data": {"user_id": account.id}},
        )
        return account, token

    async def get_account(self, principal: Principal | None) -> UserResponse:
        if principal is None:
            raise UnauthorizedError()
        user = await user_repo.get_user(self.db, principal.user_id)
        if user is None or not user.is_active:
            raise UnauthorizedError("The account no longer exists")
        return UserResponse.model_validate(user)

    async def resolve_principal(self, bearer: str | None) -> Principal | None:
        if not isinstance(bearer, str) or not bearer:
            return None
        if bearer.startswith(TOKEN_PREFIX):
            return await self.token_service.verify(bearer)
        user = await self.auth_provider.get_active_user_from_token(self.db, bearer)
        if user is None:
            return None
        return Principal(
            user_id=user.id, username=user.username, role=user.role, scope=TokenScope.WRITE
        )
