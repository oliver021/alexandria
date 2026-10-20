from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import OptionalPrincipal, Skills
from app.api.documentation import DocumentedRoute, KindQuery, TaxonomyLimitQuery, errors
from app.models.schemas import TaxonomyItem

router = APIRouter(tags=["taxonomy"], route_class=DocumentedRoute)


@router.get(
    "/tags",
    response_model=list[TaxonomyItem],
    summary="List tags",
    description=(
        "Returns the tags in use with the number of entries carrying each one, most used "
        "first. Only entries the caller can see are counted."
    ),
    responses=errors(401, 422),
)
async def list_tags(
    principal: OptionalPrincipal,
    service: Skills,
    kind: KindQuery = None,
    limit: TaxonomyLimitQuery = 50,
) -> list[TaxonomyItem]:
    return await service.tags(principal, kind=kind, limit=limit)


@router.get(
    "/categories",
    response_model=list[TaxonomyItem],
    summary="List categories",
    description=(
        "Returns the categories in use with the number of entries in each one, most used "
        "first. Only entries the caller can see are counted."
    ),
    responses=errors(401, 422),
)
async def list_categories(
    principal: OptionalPrincipal,
    service: Skills,
    kind: KindQuery = None,
    limit: TaxonomyLimitQuery = 50,
) -> list[TaxonomyItem]:
    return await service.categories(principal, kind=kind, limit=limit)
