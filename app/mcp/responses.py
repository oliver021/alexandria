from __future__ import annotations

from starlette.responses import JSONResponse

from app.models.schemas import ErrorDetail, ErrorResponse


def error_response(
    status_code: int, code: str, message: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    body = ErrorResponse(error=ErrorDetail(code=code, message=message)).model_dump(
        exclude_none=True
    )
    return JSONResponse(body, status_code=status_code, headers=headers)
