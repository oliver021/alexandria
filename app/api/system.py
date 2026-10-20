from __future__ import annotations

import importlib.metadata
from functools import lru_cache

from fastapi import APIRouter

from app.api.documentation import DocumentedRoute
from app.api.responses import add_head_route
from app.config.settings import settings
from app.models.schemas import HealthResponse, InfoResponse, LimitsInfo

DISTRIBUTION_NAME = "alexandria"
UNKNOWN_VERSION = "0.0.0"
REGISTRY_NAME = "Alexandria"
MCP_PATH = "/mcp"

router = APIRouter(tags=["system"], route_class=DocumentedRoute)


@lru_cache(maxsize=1)
def package_version() -> str:
    try:
        return importlib.metadata.version(DISTRIBUTION_NAME)
    except importlib.metadata.PackageNotFoundError:
        return UNKNOWN_VERSION


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness probe",
    description=(
        "Answers `ok` while the process is serving requests. Public: needs no credential. "
        "`HEAD` answers with the same headers and no body."
    ),
)
async def read_health() -> HealthResponse:
    return HealthResponse()


@router.get(
    "/info",
    response_model=InfoResponse,
    summary="Registry information",
    description=(
        "Describes this registry: its version, whether anonymous reading is enabled, the "
        "public URL used to build download links, the MCP endpoint path and the package "
        "limits that uploads must respect. Public: needs no credential. `HEAD` answers "
        "with the same headers and no body."
    ),
)
async def read_info() -> InfoResponse:
    return InfoResponse(
        name=REGISTRY_NAME,
        version=package_version(),
        public_read=settings.registry.public_read,
        public_url=settings.server.public_url,
        mcp_path=MCP_PATH,
        limits=LimitsInfo(**settings.registry.limits.model_dump()),
    )


add_head_route(router, "/health", read_health)
add_head_route(router, "/info", read_info)
