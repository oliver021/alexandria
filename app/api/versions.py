from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import OptionalPrincipal, RequiredPrincipal, Versions
from app.api.documentation import DocumentedRoute, RefPath, VersionPath, errors
from app.models.schemas import VersionDetail, VersionSummary

router = APIRouter(prefix="/skills/{ref}/versions", tags=["versions"], route_class=DocumentedRoute)


@router.get(
    "",
    response_model=list[VersionSummary],
    summary="List the versions of an entry",
    description=(
        "Returns the versions the caller may see, from the highest semantic version to the "
        "lowest. Everyone sees `published` and `deprecated` versions; owners and admins also "
        "see `draft` versions."
    ),
    responses=errors(401, 404, 422, 500),
)
async def list_versions(
    ref: RefPath, principal: OptionalPrincipal, service: Versions
) -> list[VersionSummary]:
    return await service.list(principal, ref)


@router.get(
    "/{version}",
    response_model=VersionDetail,
    summary="Get a version",
    description=(
        "Returns one version with its extracted metadata and an absolute download URL. "
        "`version` is an exact semantic version or `latest`, the highest published version "
        "(stable releases are preferred over prereleases)."
    ),
    responses=errors(401, 404, 422, 500),
)
async def get_version(
    ref: RefPath, version: VersionPath, principal: OptionalPrincipal, service: Versions
) -> VersionDetail:
    return await service.get(principal, ref, version)


@router.post(
    "/{version}/publish",
    response_model=VersionSummary,
    summary="Publish a version",
    description=(
        "Moves a `draft` or `deprecated` version to `published`. Only the owner or an admin "
        "with write scope can do it. Publishing an already published version changes nothing."
    ),
    responses=errors(401, 403, 404, 422, 500),
)
async def publish_version(
    ref: RefPath, version: VersionPath, principal: RequiredPrincipal, service: Versions
) -> VersionSummary:
    return await service.publish(principal, ref, version)


@router.post(
    "/{version}/deprecate",
    response_model=VersionSummary,
    summary="Deprecate a version",
    description=(
        "Marks a published version as `deprecated`: it stays downloadable by exact version "
        "but is never `latest`. The only published version of an entry cannot be deprecated "
        "(hide the entry instead). Only the owner or an admin with write scope can do it."
    ),
    responses=errors(401, 403, 404, 409, 422, 500),
)
async def deprecate_version(
    ref: RefPath, version: VersionPath, principal: RequiredPrincipal, service: Versions
) -> VersionSummary:
    return await service.deprecate(principal, ref, version)
