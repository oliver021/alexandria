from __future__ import annotations

import io
import json
import os
import threading
import uuid
from contextlib import redirect_stderr
from pathlib import Path

import httpx
import pytest

from app import cli
from app.cli import CliError, UsageError
from app.services.errors import InvalidPackageError
from tests.test_cli.support import skill_tree

LONE_SURROGATE = chr(0xD800)
BIDI_OVERRIDE = chr(0x202E)
ZERO_WIDTH_SPACE = chr(0x200B)


class TestCleanText:
    @pytest.mark.parametrize(
        ("raw", "clean"),
        [
            ("plain text", "plain text"),
            ("  padded \t and\nspread\r\nout  ", "padded and spread out"),
            ("\x1b[31mred\x1b[0m", "?[31mred?[0m"),
            ("title\x07bell", "title?bell"),
            ("null\x00byte", "null?byte"),
            ("del\x7fchar", "del?char"),
            ("c1\x85control", "c1 control"),
            ("c1\x9bcsi", "c1?csi"),
            (f"bidi{BIDI_OVERRIDE}override", "bidi?override"),
            (f"zero{ZERO_WIDTH_SPACE}width", "zero?width"),
            (f"lone{LONE_SURROGATE}surrogate", "lone?surrogate"),
            ("line separator here", "line separator here"),
            ("café ñandú 日本語", "café ñandú 日本語"),
            ("emoji 😀 stays", "emoji 😀 stays"),
            ("", ""),
            (None, "None"),
            (42, "42"),
        ],
    )
    def test_text_from_untrusted_sources_is_made_safe_to_print(self, raw: object, clean: str):
        assert cli._clean(raw) == clean

    def test_cleaned_text_can_always_be_encoded(self):
        hostile = "".join(map(chr, range(0x110000)))

        cleaned = cli._clean(hostile)

        cleaned.encode("utf-8")
        assert all(character.isprintable() for character in cleaned)

    def test_shown_quotes_the_cleaned_text(self):
        assert cli._shown("a\x1bb") == "'a?b'"
        assert cli._shown("x") == "'x'"


class TestHumanBytes:
    @pytest.mark.parametrize(
        ("count", "text"),
        [
            (0, "0 B"),
            (1, "1 B"),
            (1023, "1023 B"),
            (1024, "1.0 KiB"),
            (1536, "1.5 KiB"),
            (10 * 1024, "10.0 KiB"),
            (1024 * 1024 - 1, "1.0 MiB"),
            (1024 * 1024, "1.0 MiB"),
            (25 * 1024 * 1024, "25.0 MiB"),
            (1024**3, "1.0 GiB"),
            (1024**3 - 1, "1.0 GiB"),
            (5 * 1024**3, "5.0 GiB"),
            (3000 * 1024**3, "3000.0 GiB"),
        ],
    )
    def test_sizes_are_rounded_to_the_nearest_unit(self, count: int, text: str):
        assert cli._human_bytes(count) == text


class TestReferences:
    @pytest.mark.parametrize(
        ("spec", "expected"),
        [
            ("alpha", ("alpha", "latest")),
            ("alpha@latest", ("alpha", "latest")),
            ("alpha@1.2.3", ("alpha", "1.2.3")),
            ("alpha-beta-2@0.0.1", ("alpha-beta-2", "0.0.1")),
            ("alpha@1.0.0-rc.1", ("alpha", "1.0.0-rc.1")),
            ("alpha@1.0.0-alpha.beta.1", ("alpha", "1.0.0-alpha.beta.1")),
            ("a", ("a", "latest")),
            ("9", ("9", "latest")),
            ("a" * 64, ("a" * 64, "latest")),
            (
                "123e4567-e89b-12d3-a456-426614174000",
                ("123e4567-e89b-12d3-a456-426614174000", "latest"),
            ),
            (
                "123e4567-e89b-12d3-a456-426614174000@2.0.0",
                ("123e4567-e89b-12d3-a456-426614174000", "2.0.0"),
            ),
        ],
    )
    def test_valid_references(self, spec: str, expected: tuple[str, str]):
        assert cli.parse_reference(spec) == expected

    def test_a_generated_uuid_is_a_valid_reference(self):
        reference = str(uuid.uuid4())

        assert cli.parse_reference(reference) == (reference, "latest")

    @pytest.mark.parametrize(
        "spec",
        [
            "",
            "@",
            "@1.0.0",
            "alpha@",
            "alpha@1",
            "alpha@1.0",
            "alpha@1.0.0.0",
            "alpha@01.0.0",
            "alpha@v1.0.0",
            "alpha@1.0.0+build.5",
            "alpha@LATEST",
            "alpha@1.0.0@1.0.0",
            "Alpha",
            "alpha_beta",
            "alpha beta",
            "alpha/beta",
            "alpha\\beta",
            "..",
            "../alpha",
            "alpha.beta",
            "-alpha",
            "alpha-",
            "alpha--beta",
            "a" * 65,
            "alpha\n",
            "alpha\x00",
            "ålpha",
        ],
    )
    def test_invalid_references_are_usage_errors(self, spec: str):
        with pytest.raises(UsageError):
            cli.parse_reference(spec)


class TestUrls:
    @pytest.mark.parametrize(
        ("raw", "normalised"),
        [
            ("http://localhost:8000", "http://localhost:8000"),
            ("http://localhost:8000/", "http://localhost:8000"),
            ("https://example.org//", "https://example.org"),
            ("  https://example.org/a/b/  ", "https://example.org/a/b"),
            ("HTTP://EXAMPLE.ORG", "HTTP://EXAMPLE.ORG"),
            ("http://[::1]:9000/", "http://[::1]:9000"),
        ],
    )
    def test_valid_urls(self, raw: str, normalised: str):
        assert cli.normalize_url(raw) == normalised

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "localhost",
            "localhost:8000",
            "ftp://example.org",
            "file:///etc/passwd",
            "javascript:alert(1)",
            "http://",
            "http:///only/a/path",
            "http://user@example.org",
            "http://user:pw@example.org",
            "http://example.org/?q=1",
            "http://example.org/#top",
            "http://example.org:99999",
            "http://example.org:abc",
            "http://example.org:0",
            "http://[::1",
        ],
    )
    def test_unusable_urls_are_usage_errors(self, raw: str):
        with pytest.raises(UsageError):
            cli.normalize_url(raw)

    def test_credentials_in_the_url_are_never_echoed(self):
        with pytest.raises(UsageError) as raised:
            cli.normalize_url("https://alice:hunter2@example.org")

        assert "hunter2" not in raised.value.message
        assert "alice" not in raised.value.message

    @pytest.mark.parametrize(
        ("host", "loopback"),
        [
            ("localhost", True),
            ("registry.localhost", True),
            ("127.0.0.1", True),
            ("127.255.255.254", True),
            ("::1", True),
            ("0:0:0:0:0:0:0:1", True),
            ("example.org", False),
            ("localhost.example.org", False),
            ("notlocalhost", False),
            ("10.0.0.5", False),
            ("192.168.1.10", False),
            ("0.0.0.0", False),
            ("::", False),
            ("", False),
        ],
    )
    def test_loopback_detection(self, host: str, loopback: bool):
        assert cli._is_loopback(host) is loopback


class TestDownloadNames:
    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            ('attachment; filename="pkg-1.0.0.zip"', "pkg-1.0.0.zip"),
            ('attachment;filename="pkg-1.0.0.zip"', "pkg-1.0.0.zip"),
            ('attachment; filename="a_b+c.d-1.zip"', "a_b+c.d-1.zip"),
            ('inline; filename="x.zip"; size=3', "x.zip"),
            ('attachment; filename="../x.zip"', None),
            ('attachment; filename="x/y.zip"', None),
            ('attachment; filename="x.ZIP"', None),
            ('attachment; filename="x.tar"', None),
            ('attachment; filename="-x.zip"', None),
            ('attachment; filename=""', None),
            ("attachment; filename=plain.zip", None),
            ("attachment", None),
            ("", None),
            (None, None),
            ('attachment; filename="' + "a" * 300 + '.zip"', None),
        ],
    )
    def test_only_plain_zip_names_are_taken_from_the_server(
        self, header: str | None, expected: str | None
    ):
        assert cli._suggested_filename(header) == expected

    @pytest.mark.parametrize(
        ("filename", "name", "requested", "expected"),
        [
            ("pkg-1.2.3.zip", "pkg", "latest", "1.2.3"),
            ("pkg-1.2.3-rc.1.zip", "pkg", "latest", "1.2.3-rc.1"),
            ("my-pkg-1.2.3.zip", "my-pkg", "latest", "1.2.3"),
            ("my-pkg-1.2.3.zip", "pkg", "latest", "latest"),
            ("other-1.2.3.zip", "pkg", "latest", "latest"),
            ("pkg-1.2.zip", "pkg", "latest", "latest"),
            ("pkg-latest.zip", "pkg", "latest", "latest"),
            ("pkg-1.2.3.zip", "pkg", "1.2.3", "1.2.3"),
            (None, "pkg", "latest", "latest"),
            ("whatever-4.5.6.zip", "123e4567-e89b-12d3-a456-426614174000", "latest", "4.5.6"),
            (
                "a-b-c-0.0.1-beta.zip",
                "123e4567-e89b-12d3-a456-426614174000",
                "latest",
                "0.0.1-beta",
            ),
        ],
    )
    def test_the_resolved_version_comes_from_the_name_of_the_download(
        self, filename: str | None, name: str, requested: str, expected: str
    ):
        assert cli._resolved_version(filename, name, requested) == expected


class TestServerErrors:
    @staticmethod
    def failure(response: httpx.Response, *, has_token: bool = True) -> CliError:
        return cli._failure(response, has_token=has_token)

    def test_a_structured_error_keeps_code_reason_and_status(self):
        error = self.failure(
            httpx.Response(
                422,
                json={
                    "error": {"code": "INVALID_PACKAGE", "message": "bad", "reason": "UNSAFE_PATH"}
                },
            )
        )

        assert (error.code, error.message, error.reason, error.status) == (
            "INVALID_PACKAGE",
            "bad",
            "UNSAFE_PATH",
            422,
        )

    def test_missing_fields_of_a_structured_error_are_defaulted(self):
        error = self.failure(httpx.Response(409, json={"error": {"code": "CONFLICT"}}))

        assert (error.code, error.message, error.reason) == ("CONFLICT", "HTTP 409", None)

    @pytest.mark.parametrize("bad_reason", [None, "", 3, ["x"]])
    def test_an_unusable_reason_is_dropped(self, bad_reason: object):
        error = self.failure(
            httpx.Response(422, json={"error": {"code": "X", "message": "m", "reason": bad_reason}})
        )

        assert error.reason is None

    def test_the_401_hint_depends_on_whether_a_token_was_sent(self):
        response = httpx.Response(401, json={"error": {"code": "UNAUTHORIZED", "message": "nope"}})

        sent = self.failure(response, has_token=True)
        unsent = self.failure(response, has_token=False)

        assert sent.message == "nope; check that the token is valid and has not expired"
        assert unsent.message == "nope; no token was sent, pass --token or set ALX_TOKEN"

    def test_other_statuses_get_no_hint(self):
        error = self.failure(
            httpx.Response(403, json={"error": {"code": "FORBIDDEN", "message": "no"}})
        )

        assert error.message == "no"

    @pytest.mark.parametrize(
        ("detail", "expected"),
        [
            ("plain", "plain"),
            ([{"msg": "one"}, {"msg": "two"}], "one; two"),
            ([{"msg": "one"}, "junk", {"nomsg": 1}], "one"),
            ([], None),
            ([{"nomsg": 1}], None),
            (5, None),
            (None, None),
            ({"a": 1}, None),
        ],
    )
    def test_detail_messages(self, detail: object, expected: str | None):
        assert cli._detail_message(detail) == expected

    def test_redirects_are_their_own_kind_of_failure(self):
        error = self.failure(httpx.Response(308, headers={"Location": "https://x.example/\x1b[2J"}))

        assert error.code == "REDIRECT"
        assert error.status == 308
        assert "\x1b" not in error.message


class TestErrorPrinting:
    @staticmethod
    def printed(error: CliError, *, as_json: bool) -> str:
        buffer = io.StringIO()
        with redirect_stderr(buffer):
            cli._print_error(error, as_json=as_json)
        return buffer.getvalue()

    def test_plain_errors_have_no_code_prefix(self):
        assert self.printed(CliError("boom"), as_json=False) == "error: boom\n"

    def test_coded_errors_show_their_code_and_reason(self):
        error = CliError("m", code="INVALID_PACKAGE", reason="UNSAFE_PATH")

        assert self.printed(error, as_json=False) == "error: INVALID_PACKAGE (UNSAFE_PATH): m\n"
        assert self.printed(CliError("m", code="CONFLICT"), as_json=False) == "error: CONFLICT: m\n"

    def test_messages_are_made_printable(self):
        assert self.printed(CliError("a\x1b[0m\nb"), as_json=False) == "error: a?[0m b\n"

    def test_json_errors_are_single_lines(self):
        text = self.printed(CliError("m\n2", code="C", reason="R"), as_json=True)

        assert text.count("\n") == 1
        assert json.loads(text) == {"error": {"code": "C", "message": "m\n2", "reason": "R"}}

    def test_json_errors_omit_a_missing_reason(self):
        assert json.loads(self.printed(CliError("m"), as_json=True)) == {
            "error": {"code": "ERROR", "message": "m"}
        }

    def test_package_errors_keep_their_reason(self):
        error = cli._invalid_package(InvalidPackageError("TOO_LARGE", "too big"))

        assert (error.code, error.reason, error.message) == (
            "INVALID_PACKAGE",
            "TOO_LARGE",
            "too big",
        )


class TestFileSelection:
    @pytest.mark.parametrize(
        ("name", "excluded"),
        [
            (".git", True),
            ("__pycache__", True),
            (".DS_Store", True),
            ("node_modules", True),
            ("Thumbs.db", True),
            ("module.pyc", True),
            (".pyc", True),
            ("deep.cpython-314.pyc", True),
            ("pyc", False),
            ("module.py", False),
            ("module.pyo", False),
            (".gitignore", False),
            (".github", False),
            ("git", False),
            ("node_modules_backup", False),
            ("Thumbs.db.bak", False),
            ("thumbs.db", False),
            ("NODE_MODULES", False),
        ],
    )
    def test_exclusions_match_exact_names_and_the_pyc_suffix(self, name: str, excluded: bool):
        assert cli._is_excluded(name) is excluded


class TestPackingLimits:
    def build(self, tmp_path: Path, **limits: int):
        source = skill_tree(tmp_path / "pkg", files={"a.bin": b"x" * 100, "b.bin": b"y" * 100})
        return source, cli.PackageLimits(**limits)

    def test_the_total_size_is_checked_from_the_file_sizes_before_reading(self, tmp_path: Path):
        source, limits = self.build(tmp_path, max_uncompressed_bytes=250)

        with pytest.raises(InvalidPackageError) as raised:
            cli.collect_files(source, limits)

        assert raised.value.reason == "TOO_LARGE"
        assert "uncompressed limit of 250 bytes" in raised.value.message

    def test_the_number_of_files_is_checked_while_walking(self, tmp_path: Path):
        source, limits = self.build(tmp_path, max_files=4)

        with pytest.raises(InvalidPackageError) as raised:
            cli.collect_files(source, limits)

        assert raised.value.reason == "TOO_MANY_FILES"

    def test_the_size_of_one_file_is_checked_from_its_stat(self, tmp_path: Path):
        source, limits = self.build(tmp_path, max_file_bytes=150)
        (source / "c.bin").write_bytes(b"z" * 151)

        with pytest.raises(InvalidPackageError) as raised:
            cli.collect_files(source, limits)

        assert raised.value.reason == "TOO_LARGE"
        assert "'c.bin'" in raised.value.message

    def test_a_file_that_grows_while_packing_is_caught_when_it_is_read(self, tmp_path: Path):
        source, limits = self.build(tmp_path, max_file_bytes=150)
        item = cli.SourceFile("grown.bin", str(source / "a.bin"), False, 1)
        (source / "a.bin").write_bytes(b"x" * 151)

        with pytest.raises(InvalidPackageError) as raised:
            cli._read_source(item, limits)

        assert raised.value.reason == "TOO_LARGE"

    def test_a_file_that_vanishes_while_packing_is_reported(self, tmp_path: Path):
        source, limits = self.build(tmp_path)
        item = cli.SourceFile("gone.bin", str(source / "gone.bin"), False, 1)

        with pytest.raises(CliError, match="cannot read 'gone.bin'"):
            cli._read_source(item, limits)

    def test_the_package_limit_applies_to_the_compressed_archive(self, tmp_path: Path):
        source, limits = self.build(tmp_path, max_package_bytes=200)

        with pytest.raises(InvalidPackageError) as raised:
            cli.pack_directory(source, limits=limits)

        assert raised.value.reason == "TOO_LARGE"

    def test_a_zip_is_read_only_up_to_the_limit(self, tmp_path: Path):
        archive = tmp_path / "big.zip"
        archive.write_bytes(b"PK" * 1000)
        limits = cli.PackageLimits(max_package_bytes=100)

        with pytest.raises(InvalidPackageError) as raised:
            cli.load_package(archive, limits=limits)

        assert raised.value.reason == "TOO_LARGE"


class TestExclusiveCreation:
    def test_staging_files_are_created_exclusively(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(cli.secrets, "token_hex", lambda size: "0" * (2 * size))
        outside = tmp_path / "outside.txt"
        outside.write_text("precious")
        taken = tmp_path / ".0000000000000000.alx-part"
        taken.symlink_to(outside)

        with pytest.raises(FileExistsError):
            cli._Part(tmp_path)

        assert outside.read_text() == "precious"
        assert taken.is_symlink()

    def test_an_existing_staging_file_is_never_reused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(cli.secrets, "token_hex", lambda size: "0" * (2 * size))
        taken = tmp_path / ".0000000000000000.alx-part"
        taken.write_text("someone else's file")

        with pytest.raises(FileExistsError):
            cli._Part(tmp_path)

        assert taken.read_text() == "someone else's file"

    def test_a_failed_staging_file_does_not_remove_the_directories_it_created_before(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(cli.secrets, "token_hex", lambda size: "0" * (2 * size))
        (tmp_path / "made").mkdir()
        (tmp_path / "made" / ".0000000000000000.alx-part").write_text("x")

        with pytest.raises(FileExistsError):
            cli._Part(tmp_path / "made")

        assert (tmp_path / "made" / ".0000000000000000.alx-part").read_text() == "x"

    def test_a_new_directory_is_removed_when_the_staging_file_cannot_be_created(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        def refuse(*args, **kwargs):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(cli.os, "open", refuse)

        with pytest.raises(PermissionError):
            cli._Part(tmp_path / "new" / "deeper")

        assert not (tmp_path / "new").exists()

    def test_package_files_are_not_followed_when_they_turn_into_links(self, tmp_path: Path):
        secret = tmp_path / "secret.txt"
        secret.write_text("secret")
        link = tmp_path / "link.txt"
        link.symlink_to(secret)
        item = cli.SourceFile("link.txt", str(link), False, 6)

        with pytest.raises(CliError, match="cannot read 'link.txt'"):
            cli._read_source(item, cli.DEFAULT_LIMITS)

    def test_staging_files_for_extraction_are_created_exclusively(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from app.services.package_inspector import inspect_package
        from tests.helpers.packages import make_skill_zip

        data = make_skill_zip("exclusive", "1.0.0")
        parsed = inspect_package(data)
        monkeypatch.setattr(cli.secrets, "token_hex", lambda size: "0" * (2 * size))
        destination = tmp_path / "dest"
        destination.mkdir()
        planted = destination / ".0000000000000000.alx-tmp"
        planted.write_text("planted")

        with pytest.raises(CliError):
            cli.extract_package(data, parsed, destination)

        assert planted.read_text() == "planted"
        assert sorted(item.name for item in destination.iterdir()) == [planted.name]

    def test_a_zip_is_read_no_further_than_the_limit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        archive = tmp_path / "big.zip"
        archive.write_bytes(b"PK" * 100)
        reads: list[int] = []
        original = Path.open

        class Spy:
            def __init__(self, handle):
                self._handle = handle

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                self._handle.close()

            def read(self, size=-1):
                reads.append(size)
                return self._handle.read(size)

        def spying(self, *args, **kwargs):
            handle = original(self, *args, **kwargs)
            return Spy(handle) if self == archive else handle

        monkeypatch.setattr(Path, "open", spying)
        limits = cli.PackageLimits(max_package_bytes=50)

        with pytest.raises(InvalidPackageError):
            cli.load_package(archive, limits=limits)

        assert reads == [51]


class TestSecretsStayOutOfRepresentations:
    def test_the_token_is_not_part_of_the_remote_representation(self):
        remote = cli.Remote(url="https://registry.example.org", token="alx_TopSecret")

        assert "TopSecret" not in repr(remote)
        assert "registry.example.org" in repr(remote)

    def test_the_environment_is_not_part_of_the_context_representation(self):
        context = cli.Context(env={"ALX_TOKEN": "alx_TopSecret"})

        assert "TopSecret" not in repr(context)

    def test_a_traceback_with_debugging_enabled_does_not_print_the_token(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        def explode(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(cli, "load_package", explode)

        with pytest.raises(RuntimeError) as raised:
            cli.main(
                ["publish", str(tmp_path), "--token", "alx_TopSecret"],
                env={"ALX_DEBUG": "1"},
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )

        assert "TopSecret" not in str(raised.value)


class TestSpecialFilesNeverBlock:
    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
    def test_a_pipe_that_replaces_a_file_after_the_scan_does_not_hang_the_packing(
        self, tmp_path: Path
    ):
        pipe = tmp_path / "became-a-pipe"
        os.mkfifo(pipe)
        item = cli.SourceFile("became-a-pipe", str(pipe), False, 0)
        outcome: list[Exception] = []

        def read() -> None:
            try:
                cli._read_source(item, cli.DEFAULT_LIMITS)
            except (InvalidPackageError, CliError) as exc:
                outcome.append(exc)

        worker = threading.Thread(target=read, daemon=True)
        worker.start()
        worker.join(timeout=5)
        released = worker.is_alive()
        if released:
            writer = os.open(pipe, os.O_WRONLY)
            os.close(writer)
            worker.join(timeout=5)

        assert not released
        assert isinstance(outcome[0], InvalidPackageError)
        assert outcome[0].reason == "UNSAFE_PATH"

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
    def test_regular_files_are_still_read_in_full(self, tmp_path: Path):
        regular = tmp_path / "regular.bin"
        regular.write_bytes(b"x" * 100_000)
        item = cli.SourceFile("regular.bin", str(regular), False, 100_000)

        assert cli._read_source(item, cli.DEFAULT_LIMITS) == b"x" * 100_000


class TestBoundedAnswers:
    def test_a_short_answer_is_read_completely(self):
        response = httpx.Response(200, headers={"X-Test": "1"}, content=b"x" * 1000)

        bounded = cli._bounded(response)

        assert bounded.status_code == 200
        assert bounded.content == b"x" * 1000
        assert bounded.headers["X-Test"] == "1"

    def test_an_answer_beyond_the_limit_is_refused(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(cli, "MAX_RESPONSE_BYTES", 100)

        with pytest.raises(CliError) as raised:
            cli._bounded(httpx.Response(200, content=b"x" * 101))

        assert raised.value.code == "TOO_LARGE"

    def test_an_answer_exactly_at_the_limit_is_accepted(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(cli, "MAX_RESPONSE_BYTES", 100)

        assert cli._bounded(httpx.Response(200, content=b"x" * 100)).content == b"x" * 100

    def test_the_limit_is_enforced_while_streaming_not_after(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(cli, "MAX_RESPONSE_BYTES", 150_000)
        produced = []

        class Endless(httpx.SyncByteStream):
            def __iter__(self):
                while True:
                    produced.append(1)
                    yield b"x" * 65_536

        with pytest.raises(CliError):
            cli._bounded(httpx.Response(200, stream=Endless()))

        assert len(produced) <= 4

    def test_deeply_nested_json_is_not_an_answer(self):
        response = httpx.Response(200, content=b"[" * 200_000 + b"]" * 200_000)

        assert cli._json_body(response) is None

    @pytest.mark.parametrize("content", [b"", b"{", b"\xff\xfe", b"nope"])
    def test_unreadable_json_is_not_an_answer(self, content: bytes):
        assert cli._json_body(httpx.Response(200, content=content)) is None
