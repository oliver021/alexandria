from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect
from starlette.responses import Response

from app.logging import get_logger
from app.models.schemas import ErrorDetail, ErrorResponse
from app.services.errors import (
    ConflictError,
    DomainError,
    ForbiddenError,
    InvalidPackageError,
    NotFoundError,
    PayloadTooLargeError,
    StorageError,
    UnauthorizedError,
    ValidationError,
)

logger = get_logger("app.api.errors")

BEARER_CHALLENGE = "Bearer"
INTERNAL_ERROR_CODE = "INTERNAL_ERROR"
INTERNAL_ERROR_MESSAGE = "An internal error occurred"
STORAGE_ERROR_MESSAGE = "The stored package data could not be served"
CLIENT_DISCONNECT_MESSAGE = "The client closed the connection before the request was complete"
BODYLESS_STATUSES = frozenset({204, 304})
MAX_REPORTED_FIELD_ERRORS = 10
MAX_FIELD_MESSAGE_LENGTH = 200

DOMAIN_ERRORS: dict[type[DomainError], tuple[int, str]] = {
    NotFoundError: (404, "NOT_FOUND"),
    ConflictError: (409, "CONFLICT"),
    ValidationError: (422, "VALIDATION_ERROR"),
    InvalidPackageError: (422, "INVALID_PACKAGE"),
    ForbiddenError: (403, "FORBIDDEN"),
    UnauthorizedError: (401, "UNAUTHORIZED"),
    PayloadTooLargeError: (413, "PAYLOAD_TOO_LARGE"),
    StorageError: (500, "STORAGE_ERROR"),
}

HTTP_ERROR_CODES: dict[int, str] = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    406: "NOT_ACCEPTABLE",
    408: "REQUEST_TIMEOUT",
    409: "CONFLICT",
    411: "LENGTH_REQUIRED",
    413: "PAYLOAD_TOO_LARGE",
    414: "URI_TOO_LONG",
    415: "UNSUPPORTED_MEDIA_TYPE",
    422: "VALIDATION_ERROR",
    429: "TOO_MANY_REQUESTS",
    500: INTERNAL_ERROR_CODE,
    501: "NOT_IMPLEMENTED",
    502: "BAD_GATEWAY",
    503: "SERVICE_UNAVAILABLE",
    504: "GATEWAY_TIMEOUT",
}


def error_body(code: str, message: str, reason: str | None = None) -> dict[str, Any]:
    payload = ErrorResponse(error=ErrorDetail(code=code, message=message, reason=reason))
    return payload.model_dump(exclude_none=True)


def error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    reason: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> Response:
    response_headers = dict(headers or {})
    if status_code == 401:
        response_headers.setdefault("WWW-Authenticate", BEARER_CHALLENGE)
    if status_code in BODYLESS_STATUSES:
        return Response(status_code=status_code, headers=response_headers)
    return JSONResponse(
        error_body(code, message, reason), status_code=status_code, headers=response_headers
    )


def resolve_domain_error(exc: DomainError) -> tuple[int, str] | None:
    for cls in type(exc).__mro__:
        resolved = DOMAIN_ERRORS.get(cls)
        if resolved is not None:
            return resolved
    return None


def describe_location(location: Sequence[Any]) -> str:
    return ".".join(str(part) for part in location) or "request"


def describe_validation_errors(errors: Sequence[Mapping[str, Any]]) -> str:
    described = [
        f"{describe_location(error.get('loc', ()))}: "
        f"{str(error.get('msg', 'Invalid value'))[:MAX_FIELD_MESSAGE_LENGTH]}"
        for error in errors[:MAX_REPORTED_FIELD_ERRORS]
    ]
    hidden = len(errors) - len(described)
    if hidden > 0:
        described.append(f"and {hidden} more")
    return "Invalid request: " + "; ".join(described)


def _request_summary(request: Request) -> str:
    return f"{request.method} {request.url.path}"


async def handle_storage_error(request: Request, exc: StorageError) -> Response:
    logger.error(
        f"Storage error while handling {_request_summary(request)}: {exc.message}",
        exc_info=exc,
    )
    status_code, code = DOMAIN_ERRORS[StorageError]
    return error_response(status_code, code, STORAGE_ERROR_MESSAGE)


async def handle_domain_error(request: Request, exc: DomainError) -> Response:
    if isinstance(exc, StorageError):
        return await handle_storage_error(request, exc)
    resolved = resolve_domain_error(exc)
    if resolved is None:
        logger.error(
            f"Unmapped {type(exc).__name__} while handling {_request_summary(request)}",
            exc_info=exc,
        )
        return error_response(500, INTERNAL_ERROR_CODE, INTERNAL_ERROR_MESSAGE)
    status_code, code = resolved
    return error_response(status_code, code, exc.message, reason=exc.reason)


async def handle_validation_error(request: Request, exc: RequestValidationError) -> Response:
    return error_response(422, "VALIDATION_ERROR", describe_validation_errors(exc.errors()))


async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> Response:
    status_code = exc.status_code
    code = HTTP_ERROR_CODES.get(status_code, f"HTTP_{status_code}")
    if status_code >= 500:
        logger.error(
            f"HTTP {status_code} while handling {_request_summary(request)}: {exc.detail}",
            exc_info=exc,
        )
        return error_response(
            status_code, code, INTERNAL_ERROR_MESSAGE, headers=dict(exc.headers or {})
        )
    message = exc.detail if isinstance(exc.detail, str) and exc.detail else code
    return error_response(status_code, code, message, headers=dict(exc.headers or {}))


async def handle_client_disconnect(request: Request, exc: ClientDisconnect) -> Response:
    logger.info(f"Client disconnected while handling {_request_summary(request)}")
    return error_response(400, "BAD_REQUEST", CLIENT_DISCONNECT_MESSAGE)


async def handle_unhandled_error(request: Request, exc: Exception) -> Response:
    logger.error(
        f"Unhandled {type(exc).__name__} while handling {_request_summary(request)}",
        exc_info=exc,
    )
    return error_response(500, INTERNAL_ERROR_CODE, INTERNAL_ERROR_MESSAGE)


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(StorageError, handle_storage_error)
    app.add_exception_handler(DomainError, handle_domain_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, handle_http_exception)
    app.add_exception_handler(ClientDisconnect, handle_client_disconnect)
    app.add_exception_handler(Exception, handle_unhandled_error)
