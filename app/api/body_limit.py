from __future__ import annotations

from collections.abc import Callable

from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.errors import error_response
from app.config.settings import settings
from app.services.mappers import API_PREFIX
from app.services.package_service import encoded_package_limit

UPLOAD_PATH = f"{API_PREFIX}/packages"
MCP_PATH = "/mcp"
DEFAULT_BODY_LIMIT = 1024 * 1024
MULTIPART_ALLOWANCE = 64 * 1024
PAYLOAD_TOO_LARGE_CODE = "PAYLOAD_TOO_LARGE"
CLOSE_CONNECTION = {"Connection": "close"}

BodyLimitPolicy = Callable[[str, str], int]


def is_mcp_path(path: str) -> bool:
    return path == MCP_PATH or path.startswith(f"{MCP_PATH}/")


def body_limit_for(method: str, path: str) -> int:
    max_package_bytes = settings.registry.limits.max_package_bytes
    if method == "POST" and path.rstrip("/") == UPLOAD_PATH:
        return max_package_bytes + MULTIPART_ALLOWANCE
    if method == "POST" and is_mcp_path(path):
        return max(encoded_package_limit() + MULTIPART_ALLOWANCE, DEFAULT_BODY_LIMIT)
    return DEFAULT_BODY_LIMIT


def too_large_message(limit: int) -> str:
    return f"The request body exceeds the limit of {limit} bytes for this endpoint"


class RequestBodyTooLarge(HTTPException):
    def __init__(self, limit: int) -> None:
        super().__init__(
            status_code=413, detail=too_large_message(limit), headers=dict(CLOSE_CONNECTION)
        )
        self.limit = limit


def route_path(scope: Scope) -> str:
    path = scope.get("path", "")
    root_path = scope.get("root_path", "")
    if root_path and path.startswith(root_path):
        return path[len(root_path) :] or "/"
    return path


def declared_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", ()):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


class BodyLimitMiddleware:
    def __init__(self, app: ASGIApp, policy: BodyLimitPolicy = body_limit_for) -> None:
        self.app = app
        self.policy = policy

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self.policy(scope["method"], route_path(scope))
        declared = declared_length(scope)
        if declared is not None and declared > limit:
            await self._reject(scope, receive, send, limit)
            return
        received = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise RequestBodyTooLarge(limit)
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except RequestBodyTooLarge:
            if response_started:
                raise
            await self._reject(scope, receive, send, limit)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send, limit: int) -> None:
        response = error_response(
            413, PAYLOAD_TOO_LARGE_CODE, too_large_message(limit), headers=CLOSE_CONNECTION
        )
        await response(scope, receive, send)
