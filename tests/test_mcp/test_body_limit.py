from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.mcp.body_limit import (
    CLOSE_CONNECTION,
    PAYLOAD_TOO_LARGE_CODE,
    BodyLimitMiddleware,
    declared_length,
    too_large_message,
)


@dataclass
class Run:
    status: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    app_called: bool = False
    app_messages: list[dict[str, Any]] = field(default_factory=list)
    received_from_server: int = 0
    sent: list[dict[str, Any]] = field(default_factory=list)


def reading_app(run: Run, *, rest: int = 0):
    async def app(scope, receive, send):
        run.app_called = True
        while True:
            message = await receive()
            run.app_messages.append(message)
            if message["type"] != "http.request" or not message.get("more_body"):
                break
        for _ in range(rest):
            run.app_messages.append(await receive())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    return app


async def drive(
    app,
    run: Run,
    *,
    chunks: list[bytes] | None = None,
    headers: list[tuple[bytes, bytes]] | None = None,
    scope_type: str = "http",
    disconnect_after: int | None = None,
) -> Run:
    pending = list(chunks if chunks is not None else [b""])
    delivered = 0

    async def receive():
        nonlocal delivered
        if disconnect_after is not None and delivered >= disconnect_after:
            return {"type": "http.disconnect"}
        if pending:
            delivered += 1
            run.received_from_server += 1
            body = pending.pop(0)
            return {"type": "http.request", "body": body, "more_body": bool(pending)}
        return {"type": "http.disconnect"}

    async def send(message):
        run.sent.append(message)
        if message["type"] == "http.response.start":
            run.status = message["status"]
            run.headers = {key.decode(): value.decode() for key, value in message["headers"]}
        elif message["type"] == "http.response.body":
            run.body += message.get("body", b"")

    scope = {"type": scope_type, "method": "POST", "path": "/", "headers": headers or []}
    await app(scope, receive, send)
    return run


def limited(run: Run, limit: int, **options) -> BodyLimitMiddleware:
    return BodyLimitMiddleware(reading_app(run, **options), limit)


def length(value: int) -> list[tuple[bytes, bytes]]:
    return [(b"content-length", str(value).encode())]


class TestDeclaredLength:
    @pytest.mark.parametrize(
        ("headers", "expected"),
        [
            ([(b"content-length", b"123")], 123),
            ([(b"content-length", b"0")], 0),
            ([(b"content-length", b"abc")], None),
            ([(b"content-length", b"")], None),
            ([(b"content-type", b"text/plain")], None),
            ([], None),
        ],
    )
    def test_the_declared_length_is_read_when_it_is_a_number(self, headers, expected):
        assert declared_length({"headers": headers}) == expected

    def test_a_scope_without_headers_declares_nothing(self):
        assert declared_length({}) is None


class TestWhatIsSaidWhenTheBodyIsTooLarge:
    async def test_a_declared_length_over_the_limit_is_a_json_413_and_nothing_is_read(self):
        run = Run()

        await drive(limited(run, 100), run, chunks=[b"x" * 10], headers=length(101))

        assert run.status == 413
        assert run.app_called is False
        assert run.received_from_server == 0
        assert run.headers["content-type"] == "application/json"
        assert run.headers["connection"] == "close"
        assert json.loads(run.body) == {
            "error": {"code": PAYLOAD_TOO_LARGE_CODE, "message": too_large_message(100)}
        }

    async def test_a_streamed_body_over_the_limit_is_the_same_json_413(self):
        run = Run()

        await drive(limited(run, 100), run, chunks=[b"x" * 60, b"x" * 60])

        assert run.status == 413
        assert run.app_called is False
        assert json.loads(run.body)["error"]["code"] == "PAYLOAD_TOO_LARGE"
        assert run.headers["connection"] == "close"

    async def test_a_body_that_lies_about_its_length_is_still_refused(self):
        run = Run()

        await drive(limited(run, 100), run, chunks=[b"x" * 150], headers=length(10))

        assert run.status == 413 and run.app_called is False

    async def test_reading_stops_at_the_chunk_that_crosses_the_limit(self):
        run = Run()

        await drive(limited(run, 100), run, chunks=[b"x" * 60, b"x" * 60, b"x" * 60, b"x" * 60])

        assert run.status == 413
        assert run.received_from_server == 2

    async def test_the_message_states_the_limit_of_the_request_that_was_refused(self):
        small, large = Run(), Run()

        await drive(limited(small, 10), small, chunks=[b"x" * 11])
        await drive(limited(large, 5000), large, chunks=[b"x" * 5001])

        assert "10 bytes" in json.loads(small.body)["error"]["message"]
        assert "5000 bytes" in json.loads(large.body)["error"]["message"]

    async def test_there_is_no_challenge_and_no_plain_text(self):
        run = Run()

        await drive(limited(run, 1), run, chunks=[b"xx"])

        assert "www-authenticate" not in run.headers
        assert b"Request body too large" not in run.body


class TestWhatGetsThrough:
    @pytest.mark.parametrize("size", [0, 1, 99, 100])
    async def test_a_body_up_to_the_limit_reaches_the_app_untouched(self, size):
        run = Run()

        await drive(limited(run, 100), run, chunks=[b"x" * size], headers=length(size))

        assert run.status == 200
        assert run.body == b"ok"
        assert [m["body"] for m in run.app_messages] == [b"x" * size]

    async def test_one_byte_more_than_the_limit_does_not(self):
        run = Run()

        await drive(limited(run, 100), run, chunks=[b"x" * 101])

        assert run.status == 413

    async def test_chunks_are_replayed_as_they_arrived_without_being_copied(self):
        run = Run()
        chunks = [b"alpha", b"beta", b"gamma", b""]

        await drive(limited(run, 100), run, chunks=chunks)

        assert [m["body"] for m in run.app_messages] == chunks
        assert [m["more_body"] for m in run.app_messages] == [True, True, True, False]
        for replayed, original in zip(run.app_messages, chunks, strict=True):
            assert replayed["body"] is original

    async def test_the_body_is_complete_before_the_app_starts(self):
        run = Run()
        seen_before_start: list[int] = []

        async def inner(scope, receive, send):
            seen_before_start.append(run.received_from_server)
            await reading_app(run)(scope, receive, send)

        await drive(BodyLimitMiddleware(inner, 100), run, chunks=[b"a", b"b", b"c"])

        assert seen_before_start == [3]

    async def test_a_disconnect_during_the_body_is_handed_to_the_app(self):
        run = Run()

        await drive(limited(run, 100), run, chunks=[b"a", b"b", b"c"], disconnect_after=1)

        assert run.status == 200
        assert [m["type"] for m in run.app_messages] == ["http.request", "http.disconnect"]
        assert run.app_messages[0]["body"] == b"a"

    async def test_reads_after_the_replayed_body_go_to_the_real_receive(self):
        run = Run()

        await drive(limited(run, 100, rest=2), run, chunks=[b"a"])

        assert [m["type"] for m in run.app_messages] == [
            "http.request",
            "http.disconnect",
            "http.disconnect",
        ]

    async def test_an_empty_body_without_a_length_passes(self):
        run = Run()

        await drive(limited(run, 100), run, chunks=[b""])

        assert run.status == 200
        assert run.app_messages == [{"type": "http.request", "body": b"", "more_body": False}]

    async def test_a_length_header_that_is_not_a_number_is_ignored_and_the_stream_is_counted(self):
        run = Run()

        await drive(
            limited(run, 100), run, chunks=[b"x" * 101], headers=[(b"content-length", b"lots")]
        )

        assert run.status == 413

    async def test_other_scopes_are_passed_through_without_looking_at_them(self):
        run = Run()
        seen: list[str] = []

        async def inner(scope, receive, send):
            seen.append(scope["type"])

        await drive(BodyLimitMiddleware(inner, 1), run, scope_type="lifespan", chunks=[b"x" * 50])

        assert seen == ["lifespan"]
        assert run.received_from_server == 0

    async def test_each_request_is_judged_on_its_own(self):
        middleware = BodyLimitMiddleware(reading_app(Run()), 50)

        first, second, third = Run(), Run(), Run()
        await drive(middleware, first, chunks=[b"x" * 51])
        await drive(middleware, second, chunks=[b"x" * 50])
        await drive(middleware, third, chunks=[b"x" * 51])

        assert (first.status, second.status, third.status) == (413, 200, 413)


def test_the_close_header_is_the_one_the_rest_api_sends():
    assert CLOSE_CONNECTION == {"Connection": "close"}
