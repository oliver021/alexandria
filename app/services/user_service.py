from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.password import MAX_PASSWORD_LENGTH
from app.logging import get_logger
from app.models.schemas import UserCreate, UserResponse
from app.models.user import User, UserRole
from app.repositories import user_repo
from app.services import access
from app.services.errors import ConflictError, NotFoundError, ValidationError
from app.services.principal import Principal
from app.services.transactions import write_transaction
from app.text import is_valid_text

logger = get_logger("app.services.user")


async def register_user(
    db: AsyncSession, data: UserCreate, *, created_by: str | None = None
) -> UserResponse:
    if isinstance(data.password, str) and len(data.password) > MAX_PASSWORD_LENGTH:
        raise ValidationError(f"The password must have at most {MAX_PASSWORD_LENGTH} characters")
    if not all(is_valid_text(value) for value in (data.username, data.email, data.password)):
        raise ValidationError(
            "Username, email and password must be valid text without NUL characters"
        )
    try:
        role = UserRole(data.role)
    except ValueError:
        allowed = ", ".join(member.value for member in UserRole)
        raise ValidationError(f"Invalid role. Allowed roles: {allowed}") from None
    existing = await user_repo.get_user_by_username(db, data.username)
    if existing:
        logger.warning(
            f"Duplicate username: {data.username}",
            extra={"extra_data": {"username": data.username}},
        )
        raise ConflictError(f"Username '{data.username}' already exists")
    try:
        async with write_transaction(db):
            user = await user_repo.create_user(
                db,
                username=data.username,
                email=data.email,
                password=data.password,
                role=role,
            )
            created = UserResponse.model_validate(user)
    except IntegrityError:
        raise ConflictError("A user with that username or email already exists") from None
    logger.info(
        f"User created: {created.username}",
        extra={
            "extra_data": {"user_id": created.id, "role": created.role, "created_by": created_by}
        },
    )
    return created


class UserService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list(self, principal: Principal | None) -> list[UserResponse]:
        access.require_admin(principal)
        users = await user_repo.list_users(self.db)
        return [UserResponse.model_validate(user) for user in users]

    async def get(self, principal: Principal | None, user_id: str) -> UserResponse:
        access.require_admin(principal)
        return UserResponse.model_validate(await self._require_user(user_id))

    async def create(self, principal: Principal | None, data: UserCreate) -> UserResponse:
        admin = access.require_admin(principal)
        return await register_user(self.db, data, created_by=admin.user_id)

    async def deactivate(self, principal: Principal | None, user_id: str) -> UserResponse:
        admin = access.require_admin(principal)
        user = await self._require_user(user_id)
        if user.id == admin.user_id:
            raise ConflictError("An admin cannot deactivate their own account")
        async with write_transaction(self.db):
            deactivated = await user_repo.deactivate_user(self.db, user)
            response = UserResponse.model_validate(deactivated)
        logger.info(
            f"User deactivated: {response.username}",
            extra={"extra_data": {"user_id": response.id, "deactivated_by": admin.user_id}},
        )
        return response

    async def _require_user(self, user_id: str) -> User:
        user = await user_repo.get_user(self.db, user_id) if is_valid_text(user_id) else None
        if not user:
            raise NotFoundError("User not found")
        return user
