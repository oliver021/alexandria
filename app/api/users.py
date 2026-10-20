from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import RequiredPrincipal, Users, WriterPrincipal
from app.api.documentation import DocumentedRoute, errors
from app.models.schemas import UserCreate, UserResponse

router = APIRouter(prefix="/users", tags=["users"], route_class=DocumentedRoute)


@router.get(
    "",
    response_model=list[UserResponse],
    summary="List users",
    description="Returns every account. Admin only, with write scope.",
    responses=errors(401, 403),
)
async def list_users(principal: RequiredPrincipal, service: Users) -> list[UserResponse]:
    return await service.list(principal)


@router.post(
    "",
    status_code=201,
    response_model=UserResponse,
    summary="Create a user",
    description=(
        "Creates a local account with the role `publisher` (default) or `admin`. The password "
        "must have between 8 and 1024 characters. Admin only, with write scope."
    ),
    responses=errors(401, 403, 409, 422),
)
async def create_user(data: UserCreate, principal: WriterPrincipal, service: Users) -> UserResponse:
    return await service.create(principal, data)
