from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Response

from app.api.deps import OptionalPrincipal, RequiredPrincipal, Skills
from app.api.documentation import DocumentedRoute, KindQuery, RefPath, errors
from app.models.schemas import PaginatedResult, SkillDetail, SkillSummary
from app.services.catalog import MAX_OFFSET, MAX_PAGE_SIZE

router = APIRouter(prefix="/skills", tags=["skills"], route_class=DocumentedRoute)


@router.get(
    "",
    response_model=PaginatedResult[SkillSummary],
    summary="Search the catalog",
    description=(
        "Lists entries (skills and grounding packages). Every word of `q` must match the name, "
        "display name, description, tags or entry text. Anonymous callers and other users see "
        "published entries only; owners and admins also see their drafts and hidden entries and "
        "can filter by `status`."
    ),
    responses=errors(401, 422),
)
async def list_skills(
    principal: OptionalPrincipal,
    service: Skills,
    q: Annotated[
        str | None,
        Query(description="Free-text search; all words must match (case-insensitive)."),
    ] = None,
    kind: KindQuery = None,
    tag: Annotated[
        list[str] | None,
        Query(description="Required tag. Repeat the parameter to require several tags."),
    ] = None,
    category: Annotated[str | None, Query(description="Exact category slug.")] = None,
    status: Annotated[
        str | None,
        Query(
            description=(
                "Entry status: `draft`, `published` or `unpublished`. Only owners and admins "
                "can see entries that are not published."
            )
        ),
    ] = None,
    mine: Annotated[
        bool, Query(description="Only entries owned by the authenticated user.")
    ] = False,
    sort: Annotated[
        str | None,
        Query(
            description=(
                "`relevance` (default with `q`), `name` (default without `q`), `updated` or "
                "`created`."
            )
        ),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE, description="Page size.")] = 20,
    offset: Annotated[
        int, Query(ge=0, le=MAX_OFFSET, description="Number of entries to skip.")
    ] = 0,
) -> PaginatedResult[SkillSummary]:
    return await service.search(
        principal,
        q=q,
        kind=kind,
        tags=tag or (),
        category=category,
        status=status,
        mine=mine,
        sort=sort,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{ref}",
    response_model=SkillDetail,
    summary="Get an entry",
    description=(
        "Returns the entry and the versions the caller may see, newest first. `ref` is the "
        "entry name or its UUID."
    ),
    responses=errors(401, 404, 422, 500),
)
async def get_skill(ref: RefPath, principal: OptionalPrincipal, service: Skills) -> SkillDetail:
    return await service.get(principal, ref)


@router.delete(
    "/{ref}",
    status_code=204,
    response_class=Response,
    summary="Hide an entry",
    description=(
        "Hides the entry (status `unpublished`) without deleting any version. Only the owner "
        "or an admin with write scope can do it. Repeating the call has no further effect; "
        "`POST /skills/{ref}/restore` undoes it."
    ),
    responses=errors(401, 403, 404, 422),
)
async def hide_skill(ref: RefPath, principal: RequiredPrincipal, service: Skills) -> Response:
    await service.hide(principal, ref)
    return Response(status_code=204)


@router.post(
    "/{ref}/restore",
    response_model=SkillDetail,
    summary="Restore a hidden entry",
    description=(
        "Makes a hidden entry visible again. Its status becomes `published` when it has a "
        "published version, otherwise `draft`. Only the owner or an admin with write scope "
        "can do it."
    ),
    responses=errors(401, 403, 404, 422, 500),
)
async def restore_skill(ref: RefPath, principal: RequiredPrincipal, service: Skills) -> SkillDetail:
    return await service.restore(principal, ref)
