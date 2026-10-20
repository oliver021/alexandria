from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from fastapi.openapi.models import HTTPBearer as HTTPBearerModel
from fastapi.security.base import SecurityBase
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.bearer import AUTHORIZATION_HEADER, extract_bearer
from app.db.session import get_db
from app.logging import get_logger
from app.services import access
from app.services.auth_service import AuthService
from app.services.errors import UnauthorizedError
from app.services.package_service import PackageService
from app.services.principal import Principal
from app.services.skill_service import SkillService
from app.services.token_service import TokenService
from app.services.user_service import UserService
from app.services.version_service import VersionService
from app.storage import BlobStore, get_blob_store

logger = get_logger("app.api.deps")


class BearerCredential(SecurityBase):
    def __init__(self) -> None:
        self.model = HTTPBearerModel(
            bearerFormat="JWT or API token",
            description=(
                "Send `Authorization: Bearer <credential>`. The credential is either the JWT "
                "returned by `POST /auth/login` or an API token (`alx_...`) created with "
                "`POST /tokens`. Read operations also accept anonymous callers while the "
                "registry has `public_read` enabled (see `GET /info`); every other operation "
                "requires the credential."
            ),
        )
        self.scheme_name = "BearerAuth"

    async def __call__(self, request: Request) -> str | None:
        return extract_bearer(request.headers.getlist(AUTHORIZATION_HEADER))


bearer_credential = BearerCredential()

DbSession = Annotated[AsyncSession, Depends(get_db, scope="function")]
Store = Annotated[BlobStore, Depends(get_blob_store)]


async def release_connection(db: AsyncSession) -> None:
    await db.rollback()


async def get_auth_service(db: DbSession) -> AuthService:
    return AuthService(db)


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]


async def get_principal(
    request: Request,
    credential: Annotated[str | None, Depends(bearer_credential)],
    auth: AuthServiceDep,
) -> Principal | None:
    if credential is None:
        return None
    principal = await auth.resolve_principal(credential)
    if principal is None:
        logger.warning(
            f"Rejected credential for {request.method} {request.url.path}",
            extra={"extra_data": {"method": request.method, "path": request.url.path}},
        )
        raise UnauthorizedError("The credential is invalid, expired or revoked")
    return principal


OptionalPrincipal = Annotated[Principal | None, Depends(get_principal)]


async def require_principal(principal: OptionalPrincipal) -> Principal:
    if principal is None:
        raise UnauthorizedError()
    return principal


RequiredPrincipal = Annotated[Principal, Depends(require_principal)]


async def require_writer(principal: RequiredPrincipal) -> Principal:
    return access.require_write(principal)


WriterPrincipal = Annotated[Principal, Depends(require_writer)]


async def get_skill_service(db: DbSession) -> SkillService:
    return SkillService(db)


async def get_version_service(db: DbSession) -> VersionService:
    return VersionService(db)


async def get_package_service(db: DbSession, store: Store) -> PackageService:
    return PackageService(db, store)


async def get_token_service(db: DbSession) -> TokenService:
    return TokenService(db)


async def get_user_service(db: DbSession) -> UserService:
    return UserService(db)


Skills = Annotated[SkillService, Depends(get_skill_service)]
Versions = Annotated[VersionService, Depends(get_version_service)]
Packages = Annotated[PackageService, Depends(get_package_service)]
Tokens = Annotated[TokenService, Depends(get_token_service)]
Users = Annotated[UserService, Depends(get_user_service)]
