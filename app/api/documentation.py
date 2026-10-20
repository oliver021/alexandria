from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import Path, Query
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from app.api import deps
from app.models.schemas import ErrorResponse
from app.services.catalog import LATEST, MAX_TAXONOMY_SIZE

ANONYMOUS_SECURITY = ({}, {deps.bearer_credential.scheme_name: []})

ERROR_DESCRIPTIONS: dict[int, str] = {
    400: "The request could not be parsed.",
    401: (
        "Authentication is required, or the credential is invalid, expired or revoked. "
        "Carries `WWW-Authenticate: Bearer`."
    ),
    403: (
        "The credential is valid but not allowed to perform this operation "
        "(read-only scope, or not the owner of the entry)."
    ),
    404: "The resource does not exist or is not visible to the caller.",
    409: "The operation conflicts with the current state of the resource.",
    413: "The request body or the package exceeds the configured limit.",
    422: (
        "The request is malformed or the package is invalid. "
        "Invalid packages and invalid paths carry a machine-readable `reason`."
    ),
    500: (
        "The stored data of the package is missing, unreadable or does not match its recorded "
        "hash or manifest, or the package could not be stored. The code is `STORAGE_ERROR`; "
        "the message is generic and the details are only written to the server log."
    ),
}

ERROR_EXAMPLES: dict[int, dict[str, Any]] = {
    500: {
        "error": {
            "code": "STORAGE_ERROR",
            "message": "The stored package data could not be served",
        }
    }
}


def errors(*statuses: int) -> dict[int | str, dict[str, Any]]:
    responses: dict[int | str, dict[str, Any]] = {}
    for status in statuses:
        response: dict[str, Any] = {
            "model": ErrorResponse,
            "description": ERROR_DESCRIPTIONS[status],
        }
        if status in ERROR_EXAMPLES:
            response["content"] = {"application/json": {"example": ERROR_EXAMPLES[status]}}
        responses[status] = response
    return responses


def dependencies_of(dependant: Dependant) -> Iterator[Dependant]:
    for sub in dependant.dependencies:
        yield sub
        yield from dependencies_of(sub)


def admits_anonymous(dependant: Dependant) -> bool:
    calls = {sub.call for sub in dependencies_of(dependant)}
    return deps.get_principal in calls and deps.require_principal not in calls


class DocumentedRoute(APIRoute):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if admits_anonymous(self.dependant):
            self.openapi_extra = {**(self.openapi_extra or {}), "security": ANONYMOUS_SECURITY}


RefPath = Annotated[
    str,
    Path(description="Name of the entry, or its UUID.", examples=["pdf-forms"]),
]
VersionPath = Annotated[
    str,
    Path(
        description=f"Exact semantic version, or `{LATEST}` for the newest published version.",
        examples=["1.2.0", LATEST],
    ),
]
FilePath = Annotated[
    str,
    Path(description="Path of a file inside the package, as listed in its manifest."),
]

KindQuery = Annotated[
    str | None,
    Query(description="Restrict to one kind: `skill` or `grounding`."),
]
TaxonomyLimitQuery = Annotated[
    int,
    Query(ge=1, le=MAX_TAXONOMY_SIZE, description="Maximum number of items to return."),
]
