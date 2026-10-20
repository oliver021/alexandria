from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import (
    DEFAULT_MAX_REQUEST_BODY_SIZE,
    TransportSecuritySettings,
)
from starlette.applications import Starlette
from starlette.types import ASGIApp, Receive, Scope, Send

from app.auth.bearer import AUTHORIZATION_HEADER, extract_bearer
from app.config.settings import settings
from app.logging import get_logger
from app.mcp import context
from app.mcp.body_limit import BodyLimitMiddleware
from app.mcp.responses import error_response
from app.mcp.server import build_server
from app.services import access
from app.services.auth_service import AuthService
from app.services.errors import UnauthorizedError
from app.services.package_service import encoded_package_limit
from app.services.principal import Principal

logger = get_logger("app.mcp.asgi")

MCP_PATH = "/mcp"
BEARER_CHALLENGE = "Bearer"
AUTHORIZATION_HEADER_NAME = AUTHORIZATION_HEADER.encode("ascii")
ENVELOPE_ALLOWANCE = 64 * 1024
SDK_BODY_LIMIT = 1 << 62

ServerFactory = Callable[[], MCPServer]


def upload_body_limit() -> int:
    return max(encoded_package_limit() + ENVELOPE_ALLOWANCE, DEFAULT_MAX_REQUEST_BODY_SIZE)


def bearer_credential(scope: Scope) -> str | None:
    return extract_bearer(
        [
            value.decode("latin-1")
            for name, value in scope["headers"]
            if name == AUTHORIZATION_HEADER_NAME
        ]
    )


async def resolve_principal(credential: str) -> Principal:
    async with context.session_scope() as db:
        principal = await AuthService(db).resolve_principal(credential)
    if principal is None:
        raise UnauthorizedError("The credential is invalid, expired or revoked")
    return principal


async def authenticate(scope: Scope) -> Principal | None:
    credential = bearer_credential(scope)
    principal = None if credential is None else await resolve_principal(credential)
    access.require_read(principal, public_read=settings.registry.public_read)
    return principal


def route_path(scope: Scope) -> str:
    path = scope.get("path", "")
    root_path = scope.get("root_path", "")
    if root_path and path.startswith(root_path):
        return path[len(root_path) :]
    return path


def endpoint_scope(scope: Scope) -> Scope:
    return {**scope, "path": "/", "raw_path": b"/", "root_path": ""}


class BearerAuthMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            principal = await authenticate(scope)
        except UnauthorizedError as error:
            logger.warning(
                f"MCP request rejected: {error.message}",
                extra={"extra_data": {"method": scope["method"]}},
            )
            response = error_response(
                401,
                error.code,
                error.message,
                headers={"WWW-Authenticate": BEARER_CHALLENGE},
            )
            await response(scope, receive, send)
            return
        limit = (
            upload_body_limit()
            if principal is not None and principal.can_write
            else DEFAULT_MAX_REQUEST_BODY_SIZE
        )
        with context.use_principal(principal):
            await BodyLimitMiddleware(self.app, limit)(scope, receive, send)


class McpGateway:
    def __init__(self, server_factory: ServerFactory = build_server, path: str = MCP_PATH) -> None:
        self._server_factory = server_factory
        self._path = path
        self._active: list[ASGIApp] = []

    @asynccontextmanager
    async def lifespan(self, app: object | None = None) -> AsyncIterator[None]:
        server = self._server_factory()
        inner = server.streamable_http_app(
            streamable_http_path="/",
            json_response=True,
            stateless_http=True,
            max_request_body_size=SDK_BODY_LIMIT,
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )
        handler = BearerAuthMiddleware(inner)
        async with server.session_manager.run():
            self._active.append(handler)
            try:
                yield
            finally:
                self._active.remove(handler)

    def serves(self, path: str) -> bool:
        return path in {"", "/", self._path, f"{self._path}/"}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            return
        if not self.serves(route_path(scope)):
            await error_response(404, "NOT_FOUND", "Not found")(scope, receive, send)
            return
        if scope["method"] != "POST":
            response = error_response(
                405,
                "METHOD_NOT_ALLOWED",
                "The MCP endpoint is stateless and only accepts POST",
                headers={"Allow": "POST"},
            )
            await response(scope, receive, send)
            return
        handler = self._active[-1] if self._active else None
        if handler is None:
            logger.error("MCP request received while the MCP server is not running")
            response = error_response(503, "SERVICE_UNAVAILABLE", "The MCP server is not running")
            await response(scope, receive, send)
            return
        await handler(endpoint_scope(scope), receive, send)


mcp_app = McpGateway()
mcp_lifespan = mcp_app.lifespan


def mount_mcp(application: Starlette, gateway: McpGateway = mcp_app, path: str = MCP_PATH) -> None:
    application.mount(path, gateway, name="mcp")
    application.add_route(path, gateway, name="mcp-exact", include_in_schema=False)
