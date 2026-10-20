from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse

from app.api.deps import OptionalPrincipal, Packages
from app.api.documentation import DocumentedRoute, FilePath, RefPath, VersionPath, errors
from app.api.responses import (
    RAW_BINARY_MEDIA_TYPE,
    RAW_SECURITY_HEADERS,
    RAW_TEXT_MEDIA_TYPE,
    ClosingStreamingResponse,
    add_head_route,
    basename,
    content_disposition,
    discard_stream,
    download_cache_control,
    etag_matches,
    quoted_etag,
)
from app.models.schemas import EntrypointContent, FileContent, FileListing

router = APIRouter(
    prefix="/skills/{ref}/versions/{version}", tags=["files"], route_class=DocumentedRoute
)

ZIP_MEDIA_TYPE = "application/zip"

DOWNLOAD_RESPONSES = {
    200: {
        "description": "The package exactly as it was uploaded.",
        "content": {ZIP_MEDIA_TYPE: {"schema": {"type": "string", "format": "binary"}}},
        "headers": {
            "ETag": {
                "description": "The SHA-256 of the package, quoted.",
                "schema": {"type": "string"},
            },
            "X-Content-SHA256": {
                "description": "The SHA-256 of the package as 64 hexadecimal digits.",
                "schema": {"type": "string"},
            },
            "Content-Disposition": {
                "description": "`attachment` with the file name `{name}-{version}.zip`.",
                "schema": {"type": "string"},
            },
            "Cache-Control": {
                "description": (
                    "`public, immutable` for an exact version that anonymous callers may "
                    "download, `public, no-cache` when the version requested is `latest` (the "
                    "ETag revalidates it), `private, no-store` when a credential is needed."
                ),
                "schema": {"type": "string"},
            },
        },
    },
    304: {"description": "The caller already has this package (`If-None-Match` matched the ETag)."},
}

RAW_RESPONSES = {
    200: {
        "description": (
            "The file bytes. Text files are served as `text/plain; charset=utf-8`, anything "
            "else as an `application/octet-stream` attachment. HTML, SVG and JavaScript are "
            "never served with their own media type."
        ),
        "content": {
            "text/plain": {"schema": {"type": "string"}},
            RAW_BINARY_MEDIA_TYPE: {"schema": {"type": "string", "format": "binary"}},
        },
        "headers": {
            "Content-Length": {
                "description": "The size of the file as listed in the manifest.",
                "schema": {"type": "integer"},
            },
            "X-Content-Type-Options": {
                "description": "Always `nosniff`.",
                "schema": {"type": "string"},
            },
            "Content-Security-Policy": {
                "description": "Always `default-src 'none'; sandbox`.",
                "schema": {"type": "string"},
            },
        },
    }
}


@router.get(
    "/entrypoint",
    response_model=EntrypointContent,
    summary="Read the entry point of a version",
    description=(
        "Returns `SKILL.md` (skills) or `INDEX.md` (grounding packages) together with the "
        "resolved `name`, `version` and `kind`. Text larger than the inline limit is "
        "truncated at a character boundary and flagged with `truncated`."
    ),
    responses=errors(401, 404, 422, 500),
)
async def get_entrypoint(
    ref: RefPath, version: VersionPath, principal: OptionalPrincipal, service: Packages
) -> EntrypointContent:
    return await service.entrypoint(principal, ref, version)


@router.get(
    "/files",
    response_model=FileListing,
    summary="List the files of a version",
    description="Returns the manifest of the package: path, size, SHA-256 and media type per file.",
    responses=errors(401, 404, 422, 500),
)
async def list_files(
    ref: RefPath, version: VersionPath, principal: OptionalPrincipal, service: Packages
) -> FileListing:
    return await service.manifest(principal, ref, version)


@router.get(
    "/files/{path:path}",
    response_model=FileContent,
    summary="Read a file as JSON",
    description=(
        "Returns the file metadata and, for text files, the content. Binary files come back "
        "with `content` set to `null`; fetch them from the `raw` endpoint. Paths that escape "
        "the package are rejected with reason `INVALID_PATH`."
    ),
    responses=errors(401, 404, 422, 500),
)
async def read_file(
    ref: RefPath,
    version: VersionPath,
    path: FilePath,
    principal: OptionalPrincipal,
    service: Packages,
) -> FileContent:
    return await service.read_file(principal, ref, version, path)


@router.get(
    "/raw/{path:path}",
    response_class=Response,
    summary="Download one file",
    description=(
        "Streams the bytes of one file, hardened for the registry origin: the response always "
        "carries `X-Content-Type-Options: nosniff` and "
        "`Content-Security-Policy: default-src 'none'; sandbox`, text is shown as plain text "
        "and everything else is an attachment. The content is checked against the SHA-256 "
        "of the manifest before the first byte is sent, and `Content-Length` is the size "
        "listed in the manifest."
    ),
    responses={**RAW_RESPONSES, **errors(401, 404, 422, 500)},
)
async def read_raw_file(
    ref: RefPath,
    version: VersionPath,
    path: FilePath,
    principal: OptionalPrincipal,
    service: Packages,
) -> Response:
    raw = await service.open_raw(principal, ref, version, path)
    headers = {**RAW_SECURITY_HEADERS, "Content-Length": str(raw.size)}
    if raw.text:
        media_type = RAW_TEXT_MEDIA_TYPE
        headers["Content-Disposition"] = "inline"
    else:
        media_type = RAW_BINARY_MEDIA_TYPE
        headers["Content-Disposition"] = content_disposition(basename(raw.path))
    return ClosingStreamingResponse(raw.stream, media_type=media_type, headers=headers)


@router.get(
    "/download",
    response_class=StreamingResponse,
    summary="Download the package",
    description=(
        "Streams the original zip. `ETag` and `X-Content-SHA256` carry the SHA-256 of the "
        "package; send the ETag back in `If-None-Match` to get `304 Not Modified`. "
        "`Cache-Control` is `public, immutable` for an exact version that anonymous callers "
        "may download, `public, no-cache` when `version` is `latest` (it can move, so caches "
        "revalidate with the ETag) and `private, no-store` when a credential is needed. "
        "`HEAD` answers with the same status and headers and no body, without reading the "
        "package."
    ),
    responses={**DOWNLOAD_RESPONSES, **errors(401, 404, 422, 500)},
)
async def download_package(
    ref: RefPath,
    version: VersionPath,
    request: Request,
    principal: OptionalPrincipal,
    service: Packages,
) -> Response:
    download = await service.open_download(principal, ref, version)
    info = download.info
    etag = quoted_etag(info.sha256)
    headers = {
        "ETag": etag,
        "X-Content-SHA256": info.sha256,
        "Cache-Control": download_cache_control(info.requires_auth, pinned=download.pinned),
    }
    if etag_matches(request.headers.getlist("if-none-match"), etag):
        await discard_stream(download.stream)
        return Response(status_code=304, headers=headers)
    headers["Content-Length"] = str(info.size_bytes)
    headers["Content-Disposition"] = content_disposition(download.filename)
    return ClosingStreamingResponse(download.stream, media_type=ZIP_MEDIA_TYPE, headers=headers)


add_head_route(router, "/download", download_package)
