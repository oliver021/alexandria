from __future__ import annotations

from fastapi import APIRouter, Response

from app.api.deps import AuthServiceDep, RequiredPrincipal
from app.api.documentation import DocumentedRoute, errors
from app.models.schemas import LoginRequest, LoginResponse, UserResponse

router = APIRouter(prefix="/auth", tags=["auth"], route_class=DocumentedRoute)


@router.post(
    "/login",
    response_model=LoginResponse,
    response_description="A JWT with write scope and the authenticated user.",
    summary="Log in with a username and password",
    description=(
        "Exchanges local credentials for a JWT with `write` scope that expires after the "
        "configured number of minutes. Send it as `Authorization: Bearer <token>`. For "
        "long-lived or read-only access, create an API token with `POST /tokens` instead. "
        "Any `Authorization` header sent to this endpoint is ignored."
    ),
    responses=errors(401, 422),
)
async def login(data: LoginRequest, auth: AuthServiceDep, response: Response) -> LoginResponse:
    user, token = await auth.authenticate(data.username, data.password)
    response.headers["Cache-Control"] = "no-store"
    return LoginResponse(token=token, user=user)


@router.get(
    "/me",
    response_model=UserResponse,
    summary="Describe the authenticated user",
    description="Returns the account behind the credential, whether it is a JWT or an API token.",
    responses=errors(401),
)
async def read_me(principal: RequiredPrincipal, auth: AuthServiceDep) -> UserResponse:
    return await auth.get_account(principal)
