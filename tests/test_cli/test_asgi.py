from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

import httpx
import pytest

from app.cli import pack_directory
from tests.helpers.packages import make_skill_zip
from tests.test_cli.support import parse_form, remote_env, run, skill_tree

DATA = make_skill_zip("asgi-skill", "1.0.0", files={"notes.txt": "hello"})
DIGEST = hashlib.sha256(DATA).hexdigest()
PAGE = {
    "items": [
        {
            "name": "asgi-skill",
            "kind": "skill",
            "latest_version": "1.0.0",
            "description": "Served by an ASGI application.",
            "status": "published",
        }
    ],
    "total": 1,
    "limit": 20,
    "offset": 0,
}


class Application:
    def __init__(self, responder=None):
        self.responder = responder or self.default
        self.calls: list[tuple[str, str, dict[str, str], bytes]] = []

    @staticmethod
    def default(method: str, path: str, headers: dict[str, str], body: bytes):
        if path == "/api/v1/skills":
            return 200, {"content-type": "application/json"}, json.dumps(PAGE).encode()
        if path.startswith("/api/v1/skills/asgi-skill/") and path.endswith("/download"):
            return (
                200,
                {
                    "content-type": "application/zip",
                    "content-disposition": 'attachment; filename="asgi-skill-1.0.0.zip"',
                    "x-content-sha256": DIGEST,
                },
                DATA,
            )
        return (
            404,
            {"content-type": "application/json"},
            b'{"error": {"code": "NOT_FOUND", "message": "x"}}',
        )

    async def __call__(self, scope, receive, send) -> None:
        body = b""
        more = True
        while more:
            message = await receive()
            body += message.get("body", b"")
            more = message.get("more_body", False)
        headers = {key.decode().lower(): value.decode() for key, value in scope["headers"]}
        self.calls.append((scope["method"], scope["path"], headers, body))
        status, response_headers, payload = self.responder(
            scope["method"], scope["path"], headers, body
        )
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(k.encode(), v.encode()) for k, v in response_headers.items()],
            }
        )
        await send({"type": "http.response.body", "body": payload})


def bridge_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == "alx-transport"]


class TestAsynchronousTransports:
    def test_search_works_through_an_asgi_transport(self):
        application = Application()

        result = run("search", transport=httpx.ASGITransport(app=application), env=remote_env(None))

        assert result.code == 0, result.err
        assert "asgi-skill" in result.out
        assert application.calls[0][:2] == ("GET", "/api/v1/skills")

    def test_download_and_pull_work_through_an_asgi_transport(self, tmp_path: Path):
        application = Application()
        transport = httpx.ASGITransport(app=application)

        downloaded = run(
            "download", "asgi-skill", "-o", tmp_path / "a.zip", transport=transport, env={}
        )
        pulled = run("pull", "asgi-skill", "-d", tmp_path / "out", transport=transport, env={})

        assert downloaded.code == 0, downloaded.err
        assert (tmp_path / "a.zip").read_bytes() == DATA
        assert pulled.code == 0, pulled.err
        assert (tmp_path / "out" / "notes.txt").read_text() == "hello"

    def test_uploads_reach_the_application_intact(self, tmp_path: Path):
        source = skill_tree(tmp_path / "pkg", "asgi-skill", "1.0.0")
        received: list[bytes] = []

        def responder(method, path, headers, body):
            request = httpx.Request(
                method,
                "http://x" + path,
                headers={"content-type": headers["content-type"]},
                content=body,
            )
            form = parse_form(request)
            received.append(form["file"])
            payload = {
                "skill": {"name": "asgi-skill", "kind": "skill", "id": "1"},
                "version": {"version": "1.0.0", "status": "draft", "sha256": "0" * 64},
            }
            return 201, {"content-type": "application/json"}, json.dumps(payload).encode()

        application = Application(responder)

        result = run(
            "publish",
            source,
            transport=httpx.ASGITransport(app=application),
            env=remote_env("alx_token"),
        )

        assert result.code == 0, result.err
        assert received == [pack_directory(source).data]
        assert application.calls[0][2]["authorization"] == "Bearer alx_token"

    def test_errors_of_the_application_are_decoded(self):
        result = run(
            "download",
            "nothing",
            transport=httpx.ASGITransport(app=Application()),
            env={},
        )

        assert result.code == 1
        assert result.err.startswith("error: NOT_FOUND: ")

    def test_a_crashing_application_is_a_clean_failure(self):
        def crash(method, path, headers, body):
            raise RuntimeError("the application exploded")

        result = run("search", transport=httpx.ASGITransport(app=Application(crash)), env={})

        assert result.code == 1
        assert "the application exploded" in result.err
        assert "Traceback" not in result.err

    def test_no_helper_threads_outlive_the_command(self):
        before = len(bridge_threads())

        run("search", transport=httpx.ASGITransport(app=Application()), env={})

        assert len(bridge_threads()) == before

    def test_the_command_may_run_inside_a_running_event_loop(self):
        import asyncio

        async def scenario():
            return run("search", transport=httpx.ASGITransport(app=Application()), env={})

        result = asyncio.run(scenario())

        assert result.code == 0, result.err

    def test_the_same_transport_serves_several_commands(self):
        transport = httpx.ASGITransport(app=Application())

        results = [run("search", transport=transport, env={}) for _ in range(3)]

        assert [result.code for result in results] == [0, 0, 0]

    @pytest.mark.parametrize("flag", ["--json", None])
    def test_json_output_is_the_same_as_with_a_synchronous_transport(self, flag: str | None):
        application = Application()
        extra = [flag] if flag else []

        via_asgi = run("search", *extra, transport=httpx.ASGITransport(app=application), env={})

        def handler(request: httpx.Request) -> httpx.Response:
            status, headers, body = Application.default(
                request.method, request.url.path, dict(request.headers), request.content
            )
            return httpx.Response(status, headers=headers, content=body)

        via_mock = run("search", *extra, transport=httpx.MockTransport(handler), env={})

        assert via_asgi.code == via_mock.code == 0
        assert via_asgi.out == via_mock.out


class TestSynchronousTransportsAreNotBridged:
    def test_a_mock_transport_is_driven_directly(self):
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(threading.current_thread().name)
            return httpx.Response(200, json=PAGE)

        result = run("search", transport=httpx.MockTransport(handler), env={})

        assert result.code == 0
        assert calls == [threading.current_thread().name]
        assert not bridge_threads()

    def test_a_transport_that_is_only_asynchronous_is_bridged(self):
        seen: list[str] = []

        class OnlyAsync(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                seen.append(threading.current_thread().name)
                return httpx.Response(200, json=PAGE)

        result = run("search", transport=OnlyAsync(), env={})

        assert result.code == 0
        assert seen == ["alx-transport"]
        assert not bridge_threads()

    def test_an_asynchronous_transport_is_closed_with_the_command(self):
        closed: list[bool] = []

        class Closing(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, json=PAGE)

            async def aclose(self) -> None:
                closed.append(True)

        run("search", transport=Closing(), env={})

        assert closed == [True]
