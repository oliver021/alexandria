from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app import cli
from app.cli import pack_directory
from tests.helpers.packages import make_grounding_zip, make_skill_zip, skill_md
from tests.test_cli.support import (
    FakeRegistry,
    form_filename,
    parse_fields,
    parse_form,
    remote_env,
    run,
    skill_tree,
    write_tree,
)

TOKEN = "alx_write"


@pytest.fixture
def source(tmp_path: Path) -> Path:
    return skill_tree(tmp_path / "pkg", "demo-skill", "1.0.0", description="A demo.")


class TestSuccess:
    def test_publishing_a_directory_uploads_a_draft_and_reports_it(
        self, registry: FakeRegistry, source: Path
    ):
        result = run("publish", source, transport=registry.transport, env=remote_env())

        assert result.code == 0
        assert result.err == ""
        lines = result.out.splitlines()
        assert lines[0] == "Uploaded demo-skill 1.0.0 (skill)"
        fields = parse_fields(lines[1:])
        assert fields["status"] == "draft"
        assert fields["sha256"] == pack_directory(source).parsed.sha256
        assert fields["size"].endswith(", 3 files")
        assert fields["id"] == registry.entries["demo-skill"].skill_id
        assert "draft" in lines[-1] and "--publish" in lines[-1]
        assert registry.entries["demo-skill"].versions["1.0.0"].status == "draft"

    def test_the_request_is_an_authenticated_multipart_upload_of_the_packed_zip(
        self, registry: FakeRegistry, source: Path
    ):
        run("publish", source, transport=registry.transport, env=remote_env())

        assert len(registry.requests) == 1
        request = registry.last_request()
        assert (request.method, request.url.path) == ("POST", "/api/v1/packages")
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert request.headers["Accept"] == "application/json"
        assert request.headers["User-Agent"].startswith("alx/")
        assert request.headers["Content-Type"].startswith("multipart/form-data; boundary=")
        form = parse_form(request)
        assert set(form) == {"file"}
        assert form["file"] == pack_directory(source).data
        assert form_filename(request) == "demo-skill-1.0.0.zip"

    def test_the_publish_flag_publishes_in_the_same_request(
        self, registry: FakeRegistry, source: Path
    ):
        result = run("publish", source, "--publish", transport=registry.transport, env=remote_env())

        assert result.code == 0
        form = parse_form(registry.last_request())
        assert form["publish"] == b"true"
        assert "version" not in form
        lines = result.out.splitlines()
        assert parse_fields(lines[1:])["status"] == "published"
        assert "draft" not in result.out
        assert registry.entries["demo-skill"].versions["1.0.0"].status == "published"

    def test_the_version_option_is_sent_with_the_upload(self, registry: FakeRegistry, source: Path):
        result = run(
            "publish",
            source,
            "--version",
            "1.0.0",
            transport=registry.transport,
            env=remote_env(),
        )

        assert result.code == 0
        assert parse_form(registry.last_request())["version"] == b"1.0.0"

    def test_the_version_option_completes_a_package_without_frontmatter_version(
        self, registry: FakeRegistry, tmp_path: Path
    ):
        tree = write_tree(tmp_path / "pkg", {"SKILL.md": skill_md("late-version", None)})

        result = run(
            "publish",
            tree,
            "--version",
            "0.9.0",
            "--publish",
            transport=registry.transport,
            env=remote_env(),
        )

        assert result.code == 0
        assert "late-version 0.9.0" in result.out
        assert registry.entries["late-version"].versions["0.9.0"].status == "published"

    def test_a_zip_file_is_uploaded_as_it_is(self, registry: FakeRegistry, tmp_path: Path):
        archive = tmp_path / "anything.zip"
        data = make_skill_zip("zipped-skill", "2.0.0")
        archive.write_bytes(data)

        result = run("publish", archive, transport=registry.transport, env=remote_env())

        assert result.code == 0
        request = registry.last_request()
        assert parse_form(request)["file"] == data
        assert form_filename(request) == "zipped-skill-2.0.0.zip"

    def test_a_grounding_package_is_reported_as_grounding(
        self, registry: FakeRegistry, tmp_path: Path
    ):
        archive = tmp_path / "g.zip"
        archive.write_bytes(make_grounding_zip("facts", "1.0.0"))

        result = run("publish", archive, transport=registry.transport, env=remote_env())

        assert result.out.splitlines()[0] == "Uploaded facts 1.0.0 (grounding)"

    def test_json_output_is_the_server_response(self, registry: FakeRegistry, source: Path):
        result = run("publish", source, "--json", transport=registry.transport, env=remote_env())

        assert result.code == 0
        assert result.err == ""
        payload = result.json()
        assert set(payload) == {"skill", "version"}
        assert payload["skill"]["name"] == "demo-skill"
        assert payload["version"]["status"] == "draft"
        assert payload["version"]["sha256"] == pack_directory(source).parsed.sha256

    def test_publishing_the_same_directory_twice_conflicts_on_the_second_time(
        self, registry: FakeRegistry, source: Path
    ):
        first = run("publish", source, transport=registry.transport, env=remote_env())
        second = run("publish", source, transport=registry.transport, env=remote_env())

        assert first.code == 0
        assert second.code == 1
        assert second.err.startswith("error: CONFLICT: ")
        assert "already exists" in second.err
        assert parse_form(registry.requests[0])["file"] == parse_form(registry.requests[1])["file"]

    def test_the_server_may_answer_200_instead_of_201(self, registry: FakeRegistry, source: Path):
        first = run("publish", source, "--json", transport=registry.transport, env=remote_env())
        registry.raw_response = httpx.Response(200, json=first.json())

        result = run("publish", source, transport=registry.transport, env=remote_env())

        assert result.code == 0
        assert result.out.startswith("Uploaded demo-skill 1.0.0")


class TestConfiguration:
    def test_flags_take_precedence_over_the_environment(self, registry: FakeRegistry, source: Path):
        registry.tokens["alx_flag"] = "write"

        result = run(
            "publish",
            source,
            "--url",
            "http://localhost:9999",
            "--token",
            "alx_flag",
            transport=registry.transport,
            env=remote_env("alx_env", "http://localhost:1111"),
        )

        assert result.code == 0
        request = registry.last_request()
        assert request.url.port == 9999
        assert request.headers["Authorization"] == "Bearer alx_flag"

    def test_the_environment_is_used_when_there_are_no_flags(
        self, registry: FakeRegistry, source: Path
    ):
        run(
            "publish",
            source,
            transport=registry.transport,
            env=remote_env(TOKEN, "http://localhost:4321"),
        )

        request = registry.last_request()
        assert str(request.url) == "http://localhost:4321/api/v1/packages"
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"

    def test_the_default_registry_is_localhost_8000(self, registry: FakeRegistry, source: Path):
        run("publish", source, "--token", TOKEN, transport=registry.transport, env={})

        assert str(registry.last_request().url) == "http://localhost:8000/api/v1/packages"

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://registry.example.org/", "https://registry.example.org/api/v1/packages"),
            ("https://registry.example.org///", "https://registry.example.org/api/v1/packages"),
            ("  http://127.0.0.1:8000  ", "http://127.0.0.1:8000/api/v1/packages"),
            ("http://[::1]:8000", "http://[::1]:8000/api/v1/packages"),
        ],
    )
    def test_the_registry_address_is_normalised(
        self, registry: FakeRegistry, source: Path, url: str, expected: str
    ):
        result = run(
            "publish",
            source,
            "--url",
            url,
            transport=registry.transport,
            env=remote_env(),
        )

        assert result.code == 0
        assert str(registry.last_request().url) == expected

    def test_a_registry_behind_a_path_prefix_is_supported(self, source: Path):
        registry = FakeRegistry(prefix="/team/registry")

        result = run(
            "publish",
            source,
            "--url",
            "https://registry.example.org/team/registry/",
            transport=registry.transport,
            env=remote_env(),
        )

        assert result.code == 0
        assert registry.last_request().url.path == "/team/registry/api/v1/packages"

    def test_the_token_is_stripped(self, registry: FakeRegistry, source: Path):
        run(
            "publish",
            source,
            "--token",
            f"  {TOKEN}\n",
            transport=registry.transport,
            env={},
        )

        assert registry.last_request().headers["Authorization"] == f"Bearer {TOKEN}"

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://registry.example.org",
            "registry.example.org",
            "localhost:8000",
            "http://",
            "https:///path",
            "http://user:secret@registry.example.org",
            "http://registry.example.org?x=1",
            "http://registry.example.org#frag",
            "http://registry.example.org:notaport",
            "http://registry.example.org:0",
            "http://[::1",
        ],
    )
    def test_an_unusable_registry_address_is_a_usage_error(
        self, registry: FakeRegistry, source: Path, url: str
    ):
        result = run(
            "publish", source, "--url", url, transport=registry.transport, env=remote_env()
        )

        assert result.code == 2
        assert result.err.startswith("alx publish: error: ")
        assert registry.requests == []
        assert "secret" not in result.err


class TestCleartextWarning:
    def test_a_token_over_plain_http_to_a_remote_host_is_warned_about(
        self, registry: FakeRegistry, source: Path
    ):
        result = run(
            "publish",
            source,
            "--url",
            "http://registry.example.org",
            transport=registry.transport,
            env=remote_env(),
        )

        assert result.code == 0
        assert "warning: the token is sent over an unencrypted connection" in result.err
        assert TOKEN not in result.err

    def test_the_scheme_check_is_case_insensitive(self, registry: FakeRegistry, source: Path):
        result = run(
            "publish",
            source,
            "--url",
            "HTTP://registry.example.org",
            transport=registry.transport,
            env=remote_env(),
        )

        assert "unencrypted connection" in result.err

    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost:8000",
            "http://127.0.0.1:8000",
            "http://127.1.2.3:8000",
            "http://[::1]:8000",
            "http://registry.localhost:8000",
            "https://registry.example.org",
        ],
    )
    def test_loopback_and_https_addresses_are_not_warned_about(
        self, registry: FakeRegistry, source: Path, url: str
    ):
        result = run(
            "publish", source, "--url", url, transport=registry.transport, env=remote_env()
        )

        assert result.code == 0
        assert result.err == ""

    def test_reading_without_a_token_over_plain_http_is_not_warned_about(
        self, registry: FakeRegistry
    ):
        result = run(
            "search",
            "--url",
            "http://registry.example.org",
            transport=registry.transport,
            env={},
        )

        assert result.code == 0
        assert result.err == ""


class TestUsageErrors:
    @pytest.mark.parametrize("env", [{}, {"ALX_TOKEN": ""}, {"ALX_TOKEN": "   "}])
    def test_publishing_needs_a_token(
        self, registry: FakeRegistry, source: Path, env: dict[str, str]
    ):
        result = run("publish", source, transport=registry.transport, env=env)

        assert result.code == 2
        assert "ALX_TOKEN" in result.err and "--token" in result.err
        assert result.out == ""
        assert registry.requests == []

    def test_an_empty_token_flag_does_not_fall_back_to_nothing_silently(
        self, registry: FakeRegistry, source: Path
    ):
        result = run("publish", source, "--token", "", transport=registry.transport, env={})

        assert result.code == 2

    def test_an_empty_path_is_a_usage_error(self, registry: FakeRegistry):
        result = run("publish", "", transport=registry.transport, env=remote_env())

        assert result.code == 2
        assert "the path is empty" in result.err
        assert registry.requests == []

    def test_a_missing_path_is_a_usage_error(self, registry: FakeRegistry, tmp_path: Path):
        result = run(
            "publish", tmp_path / "nowhere", transport=registry.transport, env=remote_env()
        )

        assert result.code == 2
        assert registry.requests == []

    def test_an_invalid_version_option_is_a_usage_error(self, registry: FakeRegistry, source: Path):
        result = run(
            "publish", source, "--version", "v1", transport=registry.transport, env=remote_env()
        )

        assert result.code == 2
        assert registry.requests == []


class TestLocalValidationFirst:
    def test_an_invalid_directory_is_rejected_without_contacting_the_server(
        self, registry: FakeRegistry, tmp_path: Path
    ):
        tree = write_tree(tmp_path / "pkg", {"README.md": "x"})

        result = run("publish", tree, transport=registry.transport, env=remote_env())

        assert result.code == 1
        assert result.err.startswith("error: INVALID_PACKAGE (MISSING_ENTRYPOINT): ")
        assert registry.requests == []

    def test_an_invalid_zip_is_rejected_without_contacting_the_server(
        self, registry: FakeRegistry, tmp_path: Path
    ):
        archive = tmp_path / "bad.zip"
        archive.write_bytes(b"not a zip")

        result = run("publish", archive, transport=registry.transport, env=remote_env())

        assert result.code == 1
        assert "NOT_A_ZIP" in result.err
        assert registry.requests == []

    def test_a_version_option_that_contradicts_the_package_is_rejected_locally(
        self, registry: FakeRegistry, source: Path
    ):
        result = run(
            "publish",
            source,
            "--version",
            "9.9.9",
            transport=registry.transport,
            env=remote_env(),
        )

        assert result.code == 1
        assert "VERSION_MISMATCH" in result.err
        assert registry.requests == []

    def test_json_mode_reports_local_failures_as_json_on_stderr(
        self, registry: FakeRegistry, tmp_path: Path
    ):
        tree = write_tree(tmp_path / "pkg", {"README.md": "x"})

        result = run("publish", tree, "--json", transport=registry.transport, env=remote_env())

        assert result.code == 1
        assert result.out == ""
        assert result.error_json() == {
            "error": {
                "code": "INVALID_PACKAGE",
                "reason": "MISSING_ENTRYPOINT",
                "message": result.error_json()["error"]["message"],
            }
        }


class TestServerFailures:
    def test_a_wrong_token_is_reported_with_a_hint(self, registry: FakeRegistry, source: Path):
        result = run("publish", source, transport=registry.transport, env=remote_env("alx_wrong"))

        assert result.code == 1
        assert result.out == ""
        assert result.err.startswith("error: UNAUTHORIZED: Authentication required")
        assert "check that the token is valid" in result.err
        assert "alx_wrong" not in result.err

    def test_a_read_only_token_is_forbidden(self, registry: FakeRegistry, source: Path):
        result = run("publish", source, transport=registry.transport, env=remote_env("alx_read"))

        assert result.code == 1
        assert result.err.startswith("error: FORBIDDEN: ")

    def test_json_mode_prints_the_server_error_as_json_on_stderr(
        self, registry: FakeRegistry, source: Path
    ):
        result = run(
            "publish",
            source,
            "--json",
            transport=registry.transport,
            env=remote_env("alx_wrong"),
        )

        assert result.code == 1
        assert result.out == ""
        error = result.error_json()["error"]
        assert error["code"] == "UNAUTHORIZED"
        assert "reason" not in error

    @pytest.mark.parametrize(
        ("status", "code", "reason"),
        [
            (422, "INVALID_PACKAGE", "BAD_FRONTMATTER"),
            (422, "VALIDATION_ERROR", "INVALID_PATH"),
            (409, "CONFLICT", None),
            (413, "PAYLOAD_TOO_LARGE", None),
            (500, "STORAGE_ERROR", None),
        ],
    )
    def test_the_code_and_reason_of_the_server_error_are_shown(
        self, registry: FakeRegistry, source: Path, status: int, code: str, reason: str | None
    ):
        error = {"code": code, "message": "The server says no"}
        if reason:
            error["reason"] = reason
        registry.raw_response = httpx.Response(status, json={"error": error})

        result = run("publish", source, transport=registry.transport, env=remote_env())

        expected = f"{code} ({reason})" if reason else code
        assert result.code == 1
        assert result.err == f"error: {expected}: The server says no\n"

    def test_framework_style_error_bodies_are_understood(
        self, registry: FakeRegistry, source: Path
    ):
        registry.raw_response = httpx.Response(403, json={"detail": "Not authenticated"})

        result = run("publish", source, transport=registry.transport, env=remote_env())

        assert result.code == 1
        assert result.err == "error: HTTP_403: Not authenticated\n"

    def test_validation_error_lists_are_joined(self, registry: FakeRegistry, source: Path):
        registry.raw_response = httpx.Response(
            422, json={"detail": [{"msg": "Field required"}, {"msg": "Bad value"}, {"loc": []}]}
        )

        result = run("publish", source, transport=registry.transport, env=remote_env())

        assert result.err == "error: HTTP_422: Field required; Bad value\n"

    @pytest.mark.parametrize(
        "response",
        [
            httpx.Response(502, text="<html>Bad Gateway</html>"),
            httpx.Response(500, json=["unexpected"]),
            httpx.Response(500, json={"error": "flat string"}),
            httpx.Response(500, json={"detail": {"nested": True}}),
            httpx.Response(503, content=b""),
        ],
    )
    def test_unintelligible_error_bodies_give_a_generic_message(
        self, registry: FakeRegistry, source: Path, response: httpx.Response
    ):
        registry.raw_response = response

        result = run("publish", source, transport=registry.transport, env=remote_env())

        assert result.code == 1
        assert result.err == (
            f"error: HTTP_{response.status_code}: "
            f"unexpected response from the server (HTTP {response.status_code})\n"
        )
        assert "<html>" not in result.err

    @pytest.mark.parametrize(
        "body",
        [
            [],
            {"skill": {}},
            {"version": {}},
            {"skill": [], "version": {}},
            {"skill": {}, "version": "x"},
            "text",
        ],
    )
    def test_a_malformed_success_response_is_a_failure(
        self, registry: FakeRegistry, source: Path, body: object
    ):
        registry.raw_response = httpx.Response(201, json=body)

        result = run("publish", source, transport=registry.transport, env=remote_env())

        assert result.code == 1
        assert result.err == "error: BAD_RESPONSE: the server returned an unexpected response\n"

    def test_a_non_json_success_response_is_a_failure(self, registry: FakeRegistry, source: Path):
        registry.raw_response = httpx.Response(201, text="created")

        result = run("publish", source, transport=registry.transport, env=remote_env())

        assert result.code == 1
        assert "BAD_RESPONSE" in result.err

    def test_a_redirect_is_reported_and_never_followed(self, registry: FakeRegistry, source: Path):
        registry.redirect_to = "https://registry.example.org/api/v1/packages"

        result = run("publish", source, transport=registry.transport, env=remote_env())

        assert result.code == 1
        assert "redirect (HTTP 301)" in result.err
        assert "https://registry.example.org/api/v1/packages" in result.err
        assert "--url" in result.err
        assert len(registry.requests) == 1

    def test_a_redirect_without_a_location_is_still_reported(
        self, registry: FakeRegistry, source: Path
    ):
        registry.raw_response = httpx.Response(302)

        result = run("publish", source, transport=registry.transport, env=remote_env())

        assert result.err == "error: REDIRECT: the server answered with a redirect (HTTP 302)\n"


class TestNetworkFailures:
    @pytest.mark.parametrize(
        ("failure", "fragment"),
        [
            (httpx.ConnectError("connection refused"), "cannot connect to 'http://localhost:8000'"),
            (httpx.ConnectTimeout("slow"), "timed out"),
            (httpx.ReadTimeout("slow"), "timed out"),
            (httpx.ReadError("connection reset"), "network error talking to"),
            (httpx.RemoteProtocolError("bad framing"), "bad framing"),
            (httpx.UnsupportedProtocol("no such scheme"), "no such scheme"),
        ],
    )
    def test_transport_errors_are_reported_without_a_traceback(
        self, registry: FakeRegistry, source: Path, failure: Exception, fragment: str
    ):
        registry.failure = failure

        result = run("publish", source, transport=registry.transport, env=remote_env())

        assert result.code == 1
        assert result.err.startswith("error: NETWORK_ERROR: ")
        assert fragment in result.err
        assert "Traceback" not in result.err
        assert TOKEN not in result.err
        assert result.out == ""

    def test_network_errors_are_json_in_json_mode(self, registry: FakeRegistry, source: Path):
        registry.failure = httpx.ConnectError("refused")

        result = run("publish", source, "--json", transport=registry.transport, env=remote_env())

        assert result.code == 1
        assert result.error_json()["error"]["code"] == "NETWORK_ERROR"

    def test_a_real_connection_failure_is_a_clean_error(self, source: Path):
        result = run("publish", source, "--url", "http://127.0.0.1:1", env=remote_env())

        assert result.code == 1
        assert result.err.startswith("error: NETWORK_ERROR: cannot connect to")
        assert "Traceback" not in result.err


class TestTokenSecrecy:
    @pytest.mark.parametrize("flag", ["--json", None])
    def test_the_token_never_appears_in_any_output(
        self, registry: FakeRegistry, source: Path, flag: str | None
    ):
        extra = [flag] if flag else []
        secret = "alx_SuperSecretValue123"
        registry.tokens[secret] = "write"
        outputs = []
        outputs.append(
            run(
                "publish",
                source,
                "--publish",
                *extra,
                "--token",
                secret,
                transport=registry.transport,
                env={},
            )
        )
        outputs.append(
            run(
                "publish",
                source,
                *extra,
                "--token",
                secret,
                transport=registry.transport,
                env={},
            )
        )
        registry.raw_response = httpx.Response(
            401, json={"error": {"code": "UNAUTHORIZED", "message": "no"}}
        )
        outputs.append(
            run("publish", source, *extra, "--token", secret, transport=registry.transport, env={})
        )

        for result in outputs:
            assert secret not in result.out
            assert secret not in result.err


class TestHostileAnswers:
    def test_an_endless_answer_is_refused(
        self, registry: FakeRegistry, source: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(cli, "MAX_RESPONSE_BYTES", 200)
        registry.raw_response = httpx.Response(201, content=b"x" * 500)

        result = run("publish", source, transport=registry.transport, env=remote_env())

        assert result.code == 1
        assert result.err.startswith("error: TOO_LARGE: the server sent more than 200 bytes")

    def test_an_endless_error_page_is_refused_too(
        self, registry: FakeRegistry, source: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(cli, "MAX_RESPONSE_BYTES", 200)
        registry.raw_response = httpx.Response(502, content=b"<html>" + b"x" * 500)

        result = run("publish", source, transport=registry.transport, env=remote_env())

        assert result.code == 1
        assert "TOO_LARGE" in result.err

    def test_a_deeply_nested_answer_is_a_bad_answer(self, registry: FakeRegistry, source: Path):
        registry.raw_response = httpx.Response(201, content=b"[" * 200_000 + b"]" * 200_000)

        result = run("publish", source, transport=registry.transport, env=remote_env())

        assert result.code == 1
        assert "BAD_RESPONSE" in result.err

    def test_a_deeply_nested_error_body_gives_the_generic_message(
        self, registry: FakeRegistry, source: Path
    ):
        registry.raw_response = httpx.Response(500, content=b"[" * 200_000 + b"]" * 200_000)

        result = run("publish", source, transport=registry.transport, env=remote_env())

        assert result.err == "error: HTTP_500: unexpected response from the server (HTTP 500)\n"


class TestCredentialShape:
    @pytest.mark.parametrize(
        "token",
        [
            "alx_good\r\nX-Injected: yes",
            "alx_good\nbad",
            "alx good",
            "alx_\x00good",
            "alx_ñandú",
            "alx_good;drop",
            'alx_"good"',
            "alx_good,more",
        ],
    )
    def test_a_credential_that_cannot_be_a_bearer_token_is_rejected_before_any_request(
        self, registry: FakeRegistry, source: Path, token: str
    ):
        for argv, env in (
            (("--token", token), {}),
            ((), {"ALX_TOKEN": token}),
        ):
            result = run("publish", source, *argv, transport=registry.transport, env=env)

            assert result.code == 2
            assert result.err == (
                "alx publish: error: the credential contains characters that cannot be sent "
                "as a bearer token\n"
            )
            assert "good" not in result.err and "Injected" not in result.err
        assert registry.requests == []

    @pytest.mark.parametrize(
        "token",
        [
            "alx_Abc-123_xyz",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl",
            "abc+def/ghi==",
            "A" * 400,
        ],
    )
    def test_ordinary_credentials_are_accepted(
        self, registry: FakeRegistry, source: Path, token: str
    ):
        registry.tokens[token] = "write"

        result = run("publish", source, "--token", token, transport=registry.transport, env={})

        assert result.code == 0, result.err
        assert registry.last_request().headers["Authorization"] == f"Bearer {token}"

    def test_the_search_command_checks_the_credential_too(self, registry: FakeRegistry):
        result = run("search", "--token", "bad token", transport=registry.transport, env={})

        assert result.code == 2
        assert registry.requests == []
