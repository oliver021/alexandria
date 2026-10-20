from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.routing import APIRoute
from starlette.responses import Response, StreamingResponse
from starlette.types import Receive, Scope, Send

CONTENT_DISPOSITION_FALLBACK = "download"
PRINTABLE_ASCII = range(0x20, 0x7F)
UNSAFE_QUOTED_CHARACTERS = frozenset('"\\%/')

PUBLIC_CACHE_CONTROL = "public, immutable"
REVALIDATE_CACHE_CONTROL = "public, no-cache"
PRIVATE_CACHE_CONTROL = "private, no-store"

RAW_TEXT_MEDIA_TYPE = "text/plain; charset=utf-8"
RAW_BINARY_MEDIA_TYPE = "application/octet-stream"
RAW_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; sandbox",
}


def ascii_fallback(filename: str) -> str:
    cleaned = "".join(
        char if ord(char) in PRINTABLE_ASCII and char not in UNSAFE_QUOTED_CHARACTERS else "_"
        for char in filename
    ).strip()
    return cleaned or CONTENT_DISPOSITION_FALLBACK


def content_disposition(filename: str, disposition: str = "attachment") -> str:
    fallback = ascii_fallback(filename)
    header = f'{disposition}; filename="{fallback}"'
    if filename and fallback != filename:
        header += f"; filename*=UTF-8''{quote(filename, safe='', errors='replace')}"
    return header


def quoted_etag(sha256: str) -> str:
    return f'"{sha256}"'


def etag_matches(header_values: Iterable[str], etag: str) -> bool:
    for header_value in header_values:
        for candidate in header_value.split(","):
            candidate = candidate.strip()
            if candidate == "*":
                return True
            candidate = candidate.removeprefix("W/")
            if candidate == etag:
                return True
    return False


def download_cache_control(requires_auth: bool, *, pinned: bool = True) -> str:
    if requires_auth:
        return PRIVATE_CACHE_CONTROL
    return PUBLIC_CACHE_CONTROL if pinned else REVALIDATE_CACHE_CONTROL


def basename(path: str) -> str:
    return path.rpartition("/")[2]


async def discard_stream(stream: AsyncIterator[bytes]) -> None:
    close = getattr(stream, "aclose", None)
    if close is not None:
        await close()


class ClosingStreamingResponse(StreamingResponse):
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await discard_stream(self.body_iterator)


class HeadRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        handler = super().get_route_handler()

        async def head_handler(request: Request) -> Response:
            response = await handler(request)
            body = getattr(response, "body_iterator", None)
            if body is not None:
                await discard_stream(body)
            head = Response(status_code=response.status_code)
            head.raw_headers = list(response.raw_headers)
            return head

        return head_handler


def add_head_route(router: APIRouter, path: str, endpoint: Callable[..., Any]) -> None:
    router.add_api_route(
        path,
        endpoint,
        methods=["HEAD"],
        include_in_schema=False,
        route_class_override=HeadRoute,
        name=f"head_{endpoint.__name__}",
    )
