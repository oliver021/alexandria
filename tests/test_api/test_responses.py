from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from starlette.requests import ClientDisconnect

from app.api.responses import (
    PRIVATE_CACHE_CONTROL,
    PUBLIC_CACHE_CONTROL,
    REVALIDATE_CACHE_CONTROL,
    ClosingStreamingResponse,
    ascii_fallback,
    basename,
    content_disposition,
    discard_stream,
    download_cache_control,
    etag_matches,
    quoted_etag,
)

SHA = "ab" * 32
ETAG = f'"{SHA}"'


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("plain.zip", 'attachment; filename="plain.zip"'),
        ("pdf-forms-1.2.0-rc.1.zip", 'attachment; filename="pdf-forms-1.2.0-rc.1.zip"'),
        ('quo"te.zip', "attachment; filename=\"quo_te.zip\"; filename*=UTF-8''quo%22te.zip"),
        (
            "back\\slash.zip",
            "attachment; filename=\"back_slash.zip\"; filename*=UTF-8''back%5Cslash.zip",
        ),
        ("100%.zip", "attachment; filename=\"100_.zip\"; filename*=UTF-8''100%25.zip"),
        ("a/b.zip", "attachment; filename=\"a_b.zip\"; filename*=UTF-8''a%2Fb.zip"),
        (
            "line\r\nbreak.zip",
            "attachment; filename=\"line__break.zip\"; filename*=UTF-8''line%0D%0Abreak.zip",
        ),
        ("tab\t.zip", "attachment; filename=\"tab_.zip\"; filename*=UTF-8''tab%09.zip"),
        ("nul\x00.zip", "attachment; filename=\"nul_.zip\"; filename*=UTF-8''nul%00.zip"),
        ("café.zip", "attachment; filename=\"caf_.zip\"; filename*=UTF-8''caf%C3%A9.zip"),
        ("\U0001f600.zip", "attachment; filename=\"_.zip\"; filename*=UTF-8''%F0%9F%98%80.zip"),
        ("lone\ud800.zip", "attachment; filename=\"lone_.zip\"; filename*=UTF-8''lone%3F.zip"),
        ("   ", "attachment; filename=\"download\"; filename*=UTF-8''%20%20%20"),
        ("", 'attachment; filename="download"'),
    ],
)
def test_the_file_name_header_is_safe_for_any_name(filename: str, expected: str):
    header = content_disposition(filename)

    assert header == expected
    encoded = header.encode("latin-1")
    assert b"\r" not in encoded and b"\n" not in encoded and b"\x00" not in encoded


@pytest.mark.parametrize("filename", ["a" * 5000, "ünï" * 300, "\x7f\x1f.zip", "%s%n.zip"])
def test_even_extreme_names_stay_on_one_header_line_in_ascii(filename: str):
    header = content_disposition(filename)

    header.encode("ascii")
    assert "\r" not in header and "\n" not in header
    assert header.startswith('attachment; filename="')


def test_the_disposition_kind_can_be_inline():
    assert content_disposition("a.txt", "inline") == 'inline; filename="a.txt"'


@pytest.mark.parametrize(
    ("filename", "expected"),
    [("ok.zip", "ok.zip"), ("é", "_"), ('"\\%/', "____"), ("  ", "download"), ("", "download")],
)
def test_the_ascii_fallback_replaces_everything_unsafe(filename: str, expected: str):
    assert ascii_fallback(filename) == expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [("a.txt", "a.txt"), ("dir/a.txt", "a.txt"), ("a/b/c/d.bin", "d.bin"), ("dir/", "")],
)
def test_the_base_name_is_the_last_segment(path: str, expected: str):
    assert basename(path) == expected


def test_the_etag_is_the_quoted_digest():
    assert quoted_etag(SHA) == ETAG


@pytest.mark.parametrize(
    "header",
    [
        [ETAG],
        [f"W/{ETAG}"],
        [f'"other", {ETAG}'],
        [f'{ETAG} , "other"'],
        [f'"a","b",W/{ETAG},"c"'],
        ["*"],
        ['"nope"', ETAG],
        ["  *  "],
    ],
)
def test_every_list_that_names_the_etag_matches(header: list[str]):
    assert etag_matches(header, ETAG) is True


@pytest.mark.parametrize(
    "header",
    [
        [],
        [""],
        ["W/"],
        ['"nope"'],
        [SHA],
        [ETAG[:-1]],
        [ETAG + "x"],
        ['"' + SHA.upper() + '"'],
        ["w/" + ETAG],
        ["**"],
        ["'" + SHA + "'"],
    ],
)
def test_nothing_else_matches(header: list[str]):
    assert etag_matches(header, ETAG) is False


def test_each_header_line_is_checked():
    assert etag_matches(['"x"', '"y"', ETAG], ETAG) is True


def test_cache_control_follows_the_audience():
    assert download_cache_control(False) == PUBLIC_CACHE_CONTROL == "public, immutable"
    assert download_cache_control(True) == PRIVATE_CACHE_CONTROL == "private, no-store"


@pytest.mark.parametrize(
    ("requires_auth", "pinned", "expected"),
    [
        (False, True, "public, immutable"),
        (False, False, "public, no-cache"),
        (True, True, "private, no-store"),
        (True, False, "private, no-store"),
    ],
)
def test_cache_control_revalidates_what_can_change_and_never_shares_what_is_private(
    requires_auth: bool, pinned: bool, expected: str
):
    assert download_cache_control(requires_auth, pinned=pinned) == expected
    assert REVALIDATE_CACHE_CONTROL == "public, no-cache"


@pytest.mark.asyncio
async def test_discarding_a_stream_closes_a_generator_that_never_started():
    state = {"closed": False, "started": False}

    async def generator() -> AsyncIterator[bytes]:
        state["started"] = True
        try:
            yield b"x"
        finally:
            state["closed"] = True

    stream = generator()
    await discard_stream(stream)

    assert state == {"closed": False, "started": False}
    assert await _is_exhausted(stream)


@pytest.mark.asyncio
async def test_discarding_a_started_stream_runs_its_cleanup():
    state = {"closed": False}

    async def generator() -> AsyncIterator[bytes]:
        try:
            yield b"x"
            yield b"y"
        finally:
            state["closed"] = True

    stream = generator()
    assert await anext(stream) == b"x"
    await discard_stream(stream)

    assert state["closed"] is True


@pytest.mark.asyncio
async def test_discarding_a_stream_without_a_close_method_is_harmless():
    class Plain:
        def __aiter__(self):
            return self

        async def __anext__(self) -> bytes:
            raise StopAsyncIteration

    await discard_stream(Plain())


class Probe:
    def __init__(self, chunks: int = 5, *, fail_at: int | None = None) -> None:
        self.chunks = chunks
        self.fail_at = fail_at
        self.started = False
        self.finalized = 0

    async def stream(self) -> AsyncIterator[bytes]:
        self.started = True
        try:
            for number in range(self.chunks):
                if self.fail_at == number:
                    raise RuntimeError("the source broke")
                yield bytes([number]) * 10
        finally:
            self.finalized += 1


def asgi_scope(spec_version: str) -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec_version},
        "method": "GET",
        "path": "/",
        "headers": [],
    }


async def run_response(
    probe: Probe,
    *,
    spec_version: str = "2.3",
    leave_after: int | None = None,
    send_fails_at: int | None = None,
) -> list[dict[str, Any]]:
    response = ClosingStreamingResponse(probe.stream(), media_type="application/octet-stream")
    sent: list[dict[str, Any]] = []
    left = asyncio.Event()
    requested = False

    async def receive() -> dict[str, Any]:
        nonlocal requested
        if not requested:
            requested = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await left.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if send_fails_at is not None and len(sent) >= send_fails_at:
            raise OSError("the client went away")
        sent.append(message)
        bodies = [item for item in sent if item["type"] == "http.response.body"]
        if leave_after is not None and len(bodies) >= leave_after:
            left.set()
        await asyncio.sleep(0)

    try:
        await response(asgi_scope(spec_version), receive, send)
    finally:
        left.set()
    return sent


@pytest.mark.asyncio
@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
async def test_a_finished_response_has_delivered_everything_and_closed_its_source(spec_version):
    probe = Probe()

    sent = await run_response(probe, spec_version=spec_version)

    bodies = [item.get("body", b"") for item in sent if item["type"] == "http.response.body"]
    assert b"".join(bodies) == b"".join(bytes([n]) * 10 for n in range(5))
    assert sent[-1] == {"type": "http.response.body", "body": b"", "more_body": False}
    assert probe.finalized == 1


@pytest.mark.asyncio
async def test_a_client_that_disconnects_midway_closes_the_source_before_the_call_returns():
    probe = Probe(chunks=50)

    sent = await run_response(probe, leave_after=2)

    assert 2 <= len(sent) < 50
    assert probe.started
    assert probe.finalized == 1


@pytest.mark.asyncio
async def test_a_send_that_fails_closes_the_source_and_reports_the_disconnect():
    probe = Probe(chunks=50)

    with pytest.raises(ClientDisconnect):
        await run_response(probe, spec_version="2.4", send_fails_at=3)

    assert probe.finalized == 1


@pytest.mark.asyncio
async def test_a_source_that_breaks_propagates_its_error_and_is_closed_once():
    probe = Probe(chunks=10, fail_at=3)

    with pytest.raises(RuntimeError, match="the source broke"):
        await run_response(probe, spec_version="2.4")

    assert probe.finalized == 1


@pytest.mark.asyncio
async def test_a_source_that_never_started_is_closed_without_error():
    probe = Probe()
    response = ClosingStreamingResponse(probe.stream())

    async def refuse(message: dict[str, Any]) -> None:
        raise OSError("closed before the first message")

    async def receive() -> dict[str, Any]:
        return {"type": "http.disconnect"}

    with pytest.raises(ClientDisconnect):
        await response(asgi_scope("2.4"), receive, refuse)

    assert not probe.started
    assert probe.finalized == 0


@pytest.mark.asyncio
async def test_a_plain_async_iterator_without_aclose_is_streamed_normally():
    class Plain:
        def __init__(self) -> None:
            self.items = [b"a", b"b"]

        def __aiter__(self):
            return self

        async def __anext__(self) -> bytes:
            if not self.items:
                raise StopAsyncIteration
            return self.items.pop(0)

    response = ClosingStreamingResponse(Plain())
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    async def receive() -> dict[str, Any]:
        return {"type": "http.disconnect"}

    await response(asgi_scope("2.4"), receive, send)

    assert [item.get("body") for item in sent[1:]] == [b"a", b"b", b""]


async def _is_exhausted(stream: AsyncIterator[bytes]) -> bool:
    try:
        await anext(stream)
    except StopAsyncIteration:
        return True
    return False
