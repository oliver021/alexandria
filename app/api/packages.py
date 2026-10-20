from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel
from pydantic import ValidationError as SchemaValidationError
from starlette.datastructures import FormData, UploadFile

from app.api.deps import DbSession, Packages, WriterPrincipal, release_connection
from app.api.documentation import DocumentedRoute, errors
from app.models.schemas import UploadResult
from app.services.errors import ValidationError

UPLOAD_CHUNK_SIZE = 64 * 1024
MAX_FORM_FIELDS = 8
MAX_FORM_FIELD_BYTES = 16 * 1024
FILE_FIELD = "file"

UPLOAD_REQUEST_BODY: dict[str, Any] = {
    "required": True,
    "content": {
        "multipart/form-data": {
            "schema": {
                "type": "object",
                "required": [FILE_FIELD],
                "properties": {
                    FILE_FIELD: {
                        "type": "string",
                        "format": "binary",
                        "description": "The package: a `.zip` archive.",
                    },
                    "version": {
                        "type": "string",
                        "description": (
                            "Semantic version of the package. Required unless the entry point "
                            "declares `version`; when both are present they must match."
                        ),
                        "examples": ["1.0.0"],
                    },
                    "publish": {
                        "type": "boolean",
                        "description": (
                            "Publish the version right away. Omit it to follow the registry "
                            "default (`registry.auto_publish`); a version that is not published "
                            "stays a draft until `POST .../publish`."
                        ),
                    },
                },
            }
        }
    },
}

router = APIRouter(prefix="/packages", tags=["packages"], route_class=DocumentedRoute)


class UploadFields(BaseModel):
    version: str | None = None
    publish: bool | None = None


def form_text_fields(form: FormData) -> dict[str, str]:
    return {
        name: value
        for name, value in form.items()
        if name != FILE_FIELD and isinstance(value, str) and value != ""
    }


def parse_upload_fields(form: FormData) -> UploadFields:
    try:
        return UploadFields.model_validate(form_text_fields(form))
    except SchemaValidationError as exc:
        raise RequestValidationError(
            [
                {**error, "loc": ("body", *error["loc"])}
                for error in exc.errors(include_url=False, include_input=False)
            ]
        ) from exc


def package_part(form: FormData) -> UploadFile:
    part = form.get(FILE_FIELD)
    if not isinstance(part, UploadFile):
        raise ValidationError(
            f"A multipart/form-data body with a '{FILE_FIELD}' part holding the .zip is required"
        )
    return part


async def read_at_most(part: UploadFile, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total < max_bytes:
        chunk = await part.read(min(UPLOAD_CHUNK_SIZE, max_bytes - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


@router.post(
    "",
    status_code=201,
    response_model=UploadResult,
    summary="Upload a package",
    description=(
        "Uploads a `.zip` package as `multipart/form-data` with the parts `file`, `version` "
        "(optional) and `publish` (optional). The entry is created on first upload and owned "
        "by the uploader; later uploads add versions and must come from the owner or an admin. "
        "Versions are immutable: to change a package, upload a new version. The archive is "
        "validated before anything is stored; invalid packages answer 422 with a `reason`. "
        "Bodies larger than the package limit answer 413 without being processed. Requires a "
        "credential with `write` scope."
    ),
    response_description="The entry and the version that were stored.",
    openapi_extra={"requestBody": UPLOAD_REQUEST_BODY},
    responses=errors(400, 401, 403, 409, 413, 422, 500),
)
async def upload_package(
    request: Request, principal: WriterPrincipal, service: Packages, db: DbSession
) -> UploadResult:
    await release_connection(db)
    async with request.form(
        max_files=1, max_fields=MAX_FORM_FIELDS, max_part_size=MAX_FORM_FIELD_BYTES
    ) as form:
        fields = parse_upload_fields(form)
        data = await read_at_most(package_part(form), service.limits.max_package_bytes + 1)
    return await service.upload(principal, data, version=fields.version, publish=fields.publish)
