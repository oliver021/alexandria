from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Response

from app.api.deps import RequiredPrincipal, Tokens, WriterPrincipal
from app.api.documentation import DocumentedRoute, errors
from app.models.schemas import TokenCreate, TokenCreated, TokenResponse

router = APIRouter(prefix="/tokens", tags=["tokens"], route_class=DocumentedRoute)


@router.post(
    "",
    status_code=201,
    response_model=TokenCreated,
    response_description="The new token. Its `token` secret is shown only in this response.",
    summary="Create an API token",
    description=(
        "Creates a long-lived credential for agents and scripts. The response is the only "
        "time the clear-text `token` is shown: store it, because the registry keeps only its "
        "hash. A `read` token can never write, and a token never has more permissions than "
        "its user. Requires a credential with `write` scope."
    ),
    responses=errors(401, 403, 409, 422),
)
async def create_token(
    data: TokenCreate, principal: WriterPrincipal, service: Tokens, response: Response
) -> TokenCreated:
    created = await service.create(principal, data)
    response.headers["Cache-Control"] = "no-store"
    return created


@router.get(
    "",
    response_model=list[TokenResponse],
    summary="List your API tokens",
    description=(
        "Returns your tokens, revoked ones included, without their secret. Requires a "
        "credential with `write` scope."
    ),
    responses=errors(401, 403),
)
async def list_tokens(principal: RequiredPrincipal, service: Tokens) -> list[TokenResponse]:
    return await service.list(principal)


@router.delete(
    "/{id}",
    status_code=204,
    response_class=Response,
    summary="Revoke an API token",
    description=(
        "Revokes one of your tokens. An admin can revoke any token. Revoking an already "
        "revoked token has no further effect. Requires a credential with `write` scope."
    ),
    responses=errors(401, 403, 404, 422),
)
async def revoke_token(
    id: Annotated[str, Path(description="Identifier of the token (not the secret).")],
    principal: RequiredPrincipal,
    service: Tokens,
) -> Response:
    await service.revoke(principal, id)
    return Response(status_code=204)
