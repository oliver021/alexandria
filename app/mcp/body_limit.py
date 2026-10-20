from __future__ import annotations

from collections import deque

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.mcp.responses import error_response

PAYLOAD_TOO_LARGE_CODE = "PAYLOAD_TOO_LARGE"
CLOSE_CONNECTION = {"Connection": "close"}


def too_large_message(limit: int) -> str:
    return f"The request body exceeds the limit of {limit} bytes for this endpoint"


def declared_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", ()):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


class BodyLimitMiddleware:
    def __init__(self, app: ASGIApp, limit: int) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = declared_length(scope)
        if declared is not None and declared > self.limit:
            await self._reject(scope, receive, send)
            return
        buffered: deque[Message] = deque()
        total = 0
        while True:
            message = await receive()
            buffered.append(message)
            if message["type"] != "http.request":
                break
            total += len(message.get("body", b""))
            if total > self.limit:
                await self._reject(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        async def replay() -> Message:
            if buffered:
                return buffered.popleft()
            return await receive()

        await self.app(scope, replay, send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = error_response(
            413,
            PAYLOAD_TOO_LARGE_CODE,
            too_large_message(self.limit),
            headers=dict(CLOSE_CONNECTION),
        )
        await response(scope, receive, send)
