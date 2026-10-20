from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import UTC, datetime, timedelta

import pytest
from mcp.shared.exceptions import MCPError
from sqlalchemy import update
from starlette.responses import PlainTextResponse
from starlette.types import Receive, Scope, Send

from app.auth.bearer import DUPLICATE_HEADER_MESSAGE, MALFORMED_HEADER_MESSAGE
from app.config.settings import settings
from app.mcp.asgi import BearerAuthMiddleware
from app.mcp.context import current_principal, use_principal
from app.models import ApiToken
from app.services.auth_service import AuthService
from app.services.token_service import TokenService
from app.services.user_service import UserService
from tests.helpers.packages import make_skill_zip
from tests.test_mcp.support import (
    DRAFT,
    PDF,
    forge_jwt,
    hostile_jwts,
    open_client,
    payload_of,
    post,
    rpc,
    rpc_call,
    text_of,
    zip_to_base64,
)

INVALID_MESSAGE = "The credential is invalid, expired or revoked"


def assert_unauthorized(response, message: str | None = None) -> None:
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["content-type"] == "application/json"
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == "UNAUTHORIZED"
    if message is not None:
        assert body["error"]["message"] == message


async def tool_text(app, name: str, arguments: dict, **options) -> str:
    response = await post(app, rpc_call(name, arguments), **options)
    assert response.status_code == 200, response.text
    return response.json()["result"]["content"][0]["text"]


@pytest.mark.usefixtures("registry")
class TestWhoIsCalling:
    async def test_requests_without_a_credential_are_anonymous(self, gateway):
        response = await post(gateway, rpc("tools/list"))

        assert response.status_code == 200
        assert "www-authenticate" not in response.headers

    @pytest.mark.parametrize(
        "name", ["owner_read", "owner_write", "owner_jwt", "stranger_read", "admin_write"]
    )
    async def test_valid_credentials_are_accepted(self, gateway, credentials, name):
        response = await post(gateway, rpc("tools/list"), token=getattr(credentials, name))

        assert response.status_code == 200

    @pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER", "bEaReR"])
    async def test_the_scheme_is_case_insensitive(self, gateway, credentials, scheme):
        headers = [("authorization", f"{scheme} {credentials.owner_read}")]

        response = await post(gateway, rpc("tools/list"), headers=headers)

        assert response.status_code == 200

    @pytest.mark.parametrize(
        ("name", "can_see_the_draft"),
        [
            ("owner_read", True),
            ("owner_write", True),
            ("owner_jwt", True),
            ("admin_read", True),
            ("admin_write", True),
            ("stranger_read", False),
            ("stranger_write", False),
            (None, False),
        ],
    )
    async def test_each_credential_acts_as_its_own_user(
        self, gateway, credentials, name, can_see_the_draft
    ):
        token = None if name is None else getattr(credentials, name)

        async with open_client(gateway, token) as client:
            result = await client.call_tool("get_skill", {"name": DRAFT})

        assert result.is_error is not can_see_the_draft
        if can_see_the_draft:
            assert payload_of(result)["name"] == DRAFT
        else:
            assert text_of(result).startswith("NOT_FOUND: ")

    async def test_the_identity_of_one_request_never_leaks_into_the_next(
        self, gateway, credentials
    ):
        outcomes = []
        for token in (
            credentials.owner_write,
            None,
            credentials.admin_read,
            credentials.stranger_write,
            None,
            credentials.owner_jwt,
            None,
        ):
            text = await tool_text(gateway, "get_skill", {"name": DRAFT}, token=token)
            outcomes.append(text.startswith("NOT_FOUND"))

        assert outcomes == [False, True, False, True, True, False, True]
        assert current_principal() is None

    @pytest.mark.parametrize(
        "extra",
        [
            {"path": "/mcp?token=OWNER"},
            {"path": "/mcp?access_token=OWNER&api_key=OWNER"},
            {"headers": {"cookie": "session=OWNER; token=OWNER"}},
            {"headers": {"x-api-key": "OWNER"}},
            {"headers": {"x-access-token": "OWNER", "x-auth-token": "OWNER"}},
            {"headers": {"x-forwarded-user": "alice", "x-user": "alice", "remote-user": "alice"}},
            {"headers": {"x-principal": "alice", "x-user-id": "OWNER"}},
        ],
    )
    async def test_credentials_outside_the_authorization_header_are_ignored(
        self, gateway, credentials, extra
    ):
        path = extra.get("path", "/mcp").replace("OWNER", credentials.owner_write)
        headers = {
            key: value.replace("OWNER", credentials.owner_write)
            for key, value in extra.get("headers", {}).items()
        }

        text = await tool_text(gateway, "get_skill", {"name": DRAFT}, path=path, headers=headers)

        assert text.startswith("NOT_FOUND: ")


class TestRejectedCredentials:
    @pytest.mark.parametrize(
        "token",
        [
            "not-a-token",
            "alx_" + secrets.token_urlsafe(32),
            "alx_",
            "alx_" + "A" * 300,
            "x" * 65_536,
            "a.b.c",
            "Ñandú",
            "null",
            "undefined",
            "alx_\x7f",
        ],
    )
    async def test_credentials_nobody_issued_are_rejected(self, gateway, token):
        headers = [(b"authorization", b"Bearer " + token.encode("latin-1", "replace"))]

        response = await post(gateway, rpc("tools/list"), headers=headers)

        assert_unauthorized(response, INVALID_MESSAGE)

    @pytest.mark.parametrize(
        "label",
        [
            "expired",
            "foreign-secret",
            "alg-none",
            "hs512",
            "no-subject",
            "no-expiry",
            "numeric-subject",
            "unknown-user",
            "tampered-signature",
            "swapped-payload",
            "truncated",
            "extra-segment",
        ],
    )
    async def test_forged_or_broken_jwts_are_rejected(self, gateway, actors, label):
        forged = hostile_jwts(actors.owner.user_id, actors.admin.user_id)[label]

        response = await post(gateway, rpc("tools/list"), token=forged)

        assert_unauthorized(response, INVALID_MESSAGE)

    async def test_a_jwt_signed_with_the_right_secret_but_for_nobody_is_rejected(self, gateway):
        response = await post(
            gateway, rpc("tools/list"), token=forge_jwt("00000000-0000-0000-0000-000000000000")
        )

        assert_unauthorized(response, INVALID_MESSAGE)

    async def test_rejection_does_not_depend_on_the_rpc_method_or_body(self, gateway):
        for body in (
            rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}}),
            rpc("tools/list"),
            rpc_call("get_skill", {"name": PDF}),
            rpc("ping"),
            {"not": "json-rpc"},
        ):
            assert_unauthorized(await post(gateway, body, token="alx_" + "x" * 43))

    async def test_a_revoked_token_stops_working(
        self, gateway, credentials, actors, session_factory
    ):
        assert (
            await post(gateway, rpc("tools/list"), token=credentials.owner_write)
        ).status_code == 200
        async with session_factory() as db:
            service = TokenService(db)
            for token in await service.list(actors.owner):
                await service.revoke(actors.owner, token.id)

        for token in (credentials.owner_read, credentials.owner_write):
            assert_unauthorized(
                await post(gateway, rpc("tools/list"), token=token), INVALID_MESSAGE
            )
        assert (
            await post(gateway, rpc("tools/list"), token=credentials.stranger_read)
        ).status_code == 200

    async def test_an_expired_token_stops_working(self, gateway, credentials, session_factory):
        async with session_factory() as db:
            await db.execute(
                update(ApiToken).values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
            )
            await db.commit()

        response = await post(gateway, rpc("tools/list"), token=credentials.owner_write)

        assert_unauthorized(response, INVALID_MESSAGE)

    async def test_deactivating_a_user_invalidates_their_tokens_and_jwts(
        self, gateway, credentials, actors, session_factory
    ):
        async with session_factory() as db:
            await UserService(db).deactivate(actors.admin, actors.owner.user_id)

        for token in (credentials.owner_read, credentials.owner_write, credentials.owner_jwt):
            assert_unauthorized(await post(gateway, rpc("tools/list"), token=token))
        assert (
            await post(gateway, rpc("tools/list"), token=credentials.stranger_write)
        ).status_code == 200

    async def test_all_rejections_look_the_same_to_the_caller(
        self, gateway, credentials, actors, session_factory
    ):
        async with session_factory() as db:
            service = TokenService(db)
            for token in await service.list(actors.stranger):
                await service.revoke(actors.stranger, token.id)
        hostile = hostile_jwts(actors.owner.user_id, actors.admin.user_id)
        candidates = [
            credentials.stranger_write,
            "alx_" + secrets.token_urlsafe(32),
            "garbage",
            hostile["expired"],
            hostile["unknown-user"],
            hostile["tampered-signature"],
        ]

        bodies = [
            (await post(gateway, rpc("tools/list"), token=token)).json() for token in candidates
        ]

        assert all(body == bodies[0] for body in bodies)

    @pytest.mark.parametrize(
        ("headers", "message"),
        [
            ([("authorization", "Basic dXNlcjpwYXNzd29yZA==")], "must be 'Bearer <credential>'"),
            ([("authorization", "Bearer")], "must be 'Bearer <credential>'"),
            ([("authorization", "Bearer ")], "must be 'Bearer <credential>'"),
            ([("authorization", "")], "must be 'Bearer <credential>'"),
            ([("authorization", "Token abc")], "must be 'Bearer <credential>'"),
            ([("authorization", "Bearer abc def")], "must be 'Bearer <credential>'"),
            ([("authorization", "BearerWithoutSpace")], "must be 'Bearer <credential>'"),
            (
                [("authorization", "Bearer alx_a"), ("authorization", "Bearer alx_b")],
                "Exactly one Authorization header is allowed",
            ),
        ],
    )
    async def test_malformed_authorization_headers_are_rejected(self, gateway, headers, message):
        response = await post(gateway, rpc("tools/list"), headers=headers)

        assert_unauthorized(response)
        assert message in response.json()["error"]["message"]

    @pytest.mark.parametrize(
        "value",
        ["Basic abc", "Bearer", "Bearer ", "", "Token abc", "Bearer a b", "Bearerabc", "Bearer\tx"],
    )
    async def test_a_malformed_header_gets_exactly_the_message_of_the_shared_parser(
        self, gateway, value
    ):
        response = await post(gateway, rpc("tools/list"), headers=[("authorization", value)])

        assert_unauthorized(response, MALFORMED_HEADER_MESSAGE)

    async def test_two_headers_get_exactly_the_message_of_the_shared_parser(self, gateway):
        headers = [("authorization", "Bearer alx_a"), ("authorization", "Bearer alx_b")]

        response = await post(gateway, rpc("tools/list"), headers=headers)

        assert_unauthorized(response, DUPLICATE_HEADER_MESSAGE)

    async def test_two_headers_are_rejected_even_when_one_of_them_is_valid(
        self, gateway, credentials
    ):
        headers = [
            ("authorization", f"Bearer {credentials.owner_write}"),
            ("authorization", f"Bearer {credentials.owner_write}"),
        ]

        assert_unauthorized(await post(gateway, rpc("tools/list"), headers=headers))

    async def test_a_backend_failure_while_authenticating_is_never_treated_as_anonymous(
        self, gateway, credentials, monkeypatch
    ):
        async def broken(self, bearer):
            raise RuntimeError("the database is down")

        monkeypatch.setattr(AuthService, "resolve_principal", broken)

        with pytest.raises(RuntimeError, match="database is down"):
            await post(gateway, rpc_call("get_skill", {"name": PDF}), token=credentials.owner_read)
        assert current_principal() is None

    async def test_credentials_never_reach_the_logs(self, gateway, credentials, caplog, actors):
        invalid = "alx_" + secrets.token_urlsafe(32)
        forged = hostile_jwts(actors.owner.user_id, actors.admin.user_id)["foreign-secret"]

        with caplog.at_level(logging.DEBUG):
            await post(gateway, rpc("tools/list"), token=invalid)
            await post(gateway, rpc("tools/list"), token=forged)
            await post(gateway, rpc_call("list_tags"), token=credentials.owner_write)
            await post(gateway, rpc_call("list_tags"), token=credentials.owner_jwt)

        for secret in (invalid, forged, credentials.owner_write, credentials.owner_jwt):
            assert secret not in caplog.text
        assert any("MCP request rejected" in record.getMessage() for record in caplog.records)


class TestPrivateRegistry:
    @pytest.fixture(autouse=True)
    def private(self, monkeypatch):
        monkeypatch.setattr(settings.registry, "public_read", False)

    @pytest.mark.parametrize(
        "body",
        [
            rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}}),
            rpc("tools/list"),
            rpc_call("list_tags"),
            rpc_call("get_skill", {"name": PDF}),
            rpc_call("upload_package", {"content_base64": ""}),
        ],
    )
    async def test_anonymous_requests_get_a_401_before_any_processing(self, gateway, body):
        response = await post(gateway, body)

        assert_unauthorized(response, "Authentication is required to read this registry")

    async def test_the_sdk_client_cannot_even_connect_without_a_credential(self, gateway):
        with pytest.raises(BaseExceptionGroup) as failure:
            async with open_client(gateway):
                pytest.fail("the handshake must not succeed")

        assert failure.value.subgroup(MCPError) is not None

    @pytest.mark.usefixtures("registry")
    @pytest.mark.parametrize("name", ["owner_read", "owner_write", "owner_jwt", "stranger_read"])
    async def test_valid_credentials_still_work(self, gateway, credentials, name):
        token = getattr(credentials, name)

        async with open_client(gateway, token) as client:
            result = await client.call_tool("get_skill", {"name": PDF})

        assert payload_of(result)["name"] == PDF

    async def test_invalid_credentials_are_rejected_as_before(self, gateway):
        assert_unauthorized(
            await post(gateway, rpc("tools/list"), token="alx_" + "z" * 43), INVALID_MESSAGE
        )

    async def test_the_restriction_follows_the_setting_at_request_time(self, gateway, monkeypatch):
        assert (await post(gateway, rpc("tools/list"))).status_code == 401

        monkeypatch.setattr(settings.registry, "public_read", True)

        assert (await post(gateway, rpc("tools/list"))).status_code == 200


@pytest.mark.usefixtures("registry")
class TestWriteAuthorization:
    @pytest.mark.parametrize("name", ["owner_read", "stranger_read", "admin_read"])
    async def test_read_tokens_cannot_upload_publish_or_deprecate(
        self, gateway, credentials, session_factory, name
    ):
        token = getattr(credentials, name)
        data = zip_to_base64(make_skill_zip("by-reader", "1.0.0"))

        async with open_client(gateway, token) as client:
            upload = await client.call_tool("upload_package", {"content_base64": data})
            publish = await client.call_tool("publish_version", {"name": PDF, "version": "2.0.0"})
            deprecate = await client.call_tool(
                "deprecate_version", {"name": PDF, "version": "1.1.0"}
            )
            detail = await client.call_tool("get_skill", {"name": "by-reader"})

        for result in (upload, publish, deprecate):
            assert result.is_error is True
            assert text_of(result) == "FORBIDDEN: This credential has read-only scope"
        assert text_of(detail).startswith("NOT_FOUND")

    async def test_anonymous_callers_get_unauthorized_tool_errors_not_http_errors(self, gateway):
        async with open_client(gateway) as client:
            results = [
                await client.call_tool("upload_package", {"content_base64": "AAAA"}),
                await client.call_tool("publish_version", {"name": PDF, "version": "2.0.0"}),
                await client.call_tool("deprecate_version", {"name": PDF, "version": "1.1.0"}),
            ]

        assert [text_of(result) for result in results] == [
            "UNAUTHORIZED: Authentication required"
        ] * 3

    @pytest.mark.parametrize("name", ["owner_write", "owner_jwt", "admin_write"])
    async def test_owners_and_admins_can_manage_an_entry(self, gateway, credentials, name):
        async with open_client(gateway, getattr(credentials, name)) as client:
            published = await client.call_tool("publish_version", {"name": PDF, "version": "2.0.0"})
            deprecated = await client.call_tool(
                "deprecate_version", {"name": PDF, "version": "1.0.0"}
            )

        assert payload_of(published)["status"] == "published"
        assert payload_of(deprecated)["status"] == "deprecated"

    async def test_other_writers_cannot_change_somebody_elses_entry(
        self, gateway, credentials, session_factory
    ):
        async with open_client(gateway, credentials.stranger_write) as client:
            publish = await client.call_tool("publish_version", {"name": PDF, "version": "2.0.0"})
            deprecate = await client.call_tool(
                "deprecate_version", {"name": PDF, "version": "1.0.0"}
            )
            upload = await client.call_tool(
                "upload_package",
                {"content_base64": zip_to_base64(make_skill_zip(PDF, "9.0.0"))},
            )
            hidden_draft = await client.call_tool(
                "publish_version", {"name": DRAFT, "version": "0.1.0"}
            )

        assert (
            text_of(publish) == "FORBIDDEN: Only the owner of this entry or an admin can change it"
        )
        assert text_of(deprecate).startswith("FORBIDDEN: ")
        assert text_of(upload) == "FORBIDDEN: 'pdf-forms' belongs to another user"
        assert text_of(hidden_draft).startswith("NOT_FOUND: ")
        async with open_client(gateway, credentials.owner_write) as owner:
            detail = payload_of(await owner.call_tool("get_skill", {"name": PDF}))
        assert detail["latest_version"] == "1.1.0"
        assert "9.0.0" not in [version["version"] for version in detail["versions"]]

    async def test_a_write_token_cannot_exceed_the_permissions_of_its_user(
        self, gateway, credentials
    ):
        async with open_client(gateway, credentials.stranger_write) as client:
            result = await client.call_tool("publish_version", {"name": PDF, "version": "2.0.0"})

        assert result.is_error is True

    async def test_a_jwt_has_write_scope(self, gateway, credentials):
        data = zip_to_base64(make_skill_zip("by-jwt", "1.0.0"))

        async with open_client(gateway, credentials.owner_jwt) as client:
            result = await client.call_tool("upload_package", {"content_base64": data})

        assert payload_of(result)["skill"]["name"] == "by-jwt"


class TestPrincipalContext:
    async def test_the_principal_is_visible_inside_the_block_and_restored_afterwards(self, actors):
        assert current_principal() is None
        with use_principal(actors.owner):
            assert current_principal() == actors.owner
            with use_principal(actors.admin):
                assert current_principal() == actors.admin
            assert current_principal() == actors.owner
            with use_principal(None):
                assert current_principal() is None
            assert current_principal() == actors.owner
        assert current_principal() is None

    async def test_the_principal_is_restored_when_the_block_fails(self, actors):
        with pytest.raises(RuntimeError), use_principal(actors.owner):
            raise RuntimeError("boom")

        assert current_principal() is None

    async def test_concurrent_tasks_each_see_their_own_principal(self, actors):
        names = ["owner", "stranger", "admin", "owner_read", "stranger_read", "anonymous"]
        barrier = asyncio.Barrier(len(names))

        async def observe(name: str):
            with use_principal(actors.get(name)):
                await barrier.wait()
                await asyncio.sleep(0)
                return name, current_principal()

        results = await asyncio.gather(*(observe(name) for name in names))

        for name, seen in results:
            assert seen == actors.get(name)
        assert current_principal() is None

    async def test_a_principal_set_in_a_task_does_not_leak_to_its_parent(self, actors):
        async def child():
            with use_principal(actors.admin):
                await asyncio.sleep(0)

        await asyncio.create_task(child())

        assert current_principal() is None


class RecordingApp:
    def __init__(self) -> None:
        self.seen: list = []
        self.bodies: list[int] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.seen.append(current_principal())
        total = 0
        while True:
            message = await receive()
            total += len(message.get("body", b""))
            if not message.get("more_body", False):
                break
        self.bodies.append(total)
        await PlainTextResponse("ok")(scope, receive, send)


class TestMiddleware:
    async def test_the_resolved_principal_is_bound_while_the_inner_app_runs(
        self, credentials, actors
    ):
        inner = RecordingApp()
        app = BearerAuthMiddleware(inner)

        await post(app, rpc("ping"), token=credentials.owner_write)
        await post(app, rpc("ping"), token=credentials.admin_read)
        await post(app, rpc("ping"))

        assert [p.user_id if p else None for p in inner.seen] == [
            actors.owner.user_id,
            actors.admin.user_id,
            None,
        ]
        assert [p.scope.value if p else None for p in inner.seen] == ["write", "read", None]
        assert current_principal() is None

    async def test_a_failing_inner_app_does_not_leave_the_principal_bound(self, credentials):
        class Failing:
            async def __call__(self, scope, receive, send):
                raise RuntimeError("inner failure")

        with pytest.raises(RuntimeError, match="inner failure"):
            await post(BearerAuthMiddleware(Failing()), rpc("ping"), token=credentials.owner_write)

        assert current_principal() is None

    async def test_rejected_requests_never_reach_the_inner_app(self, credentials):
        inner = RecordingApp()
        app = BearerAuthMiddleware(inner)

        response = await post(app, rpc("ping"), token="alx_" + "q" * 43)

        assert response.status_code == 401
        assert inner.seen == []

    async def test_the_inner_app_is_not_reached_when_authentication_fails_on_a_private_registry(
        self, monkeypatch
    ):
        monkeypatch.setattr(settings.registry, "public_read", False)
        inner = RecordingApp()

        response = await post(BearerAuthMiddleware(inner), rpc("ping"))

        assert response.status_code == 401
        assert inner.seen == []
