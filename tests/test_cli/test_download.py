from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import httpx
import pytest

from app import cli
from app.services.package_inspector import PackageLimits
from tests.helpers.packages import make_skill_zip
from tests.test_cli.support import FakeRegistry, parse_fields, remote_env, run

NAME = "dl-skill"
ZIP_100 = make_skill_zip(NAME, "1.0.0", files={"a.txt": "one"})
ZIP_110 = make_skill_zip(NAME, "1.1.0", files={"a.txt": "two"})
ZIP_BETA = make_skill_zip(NAME, "2.0.0-beta.1", files={"a.txt": "three"})


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def stocked(registry: FakeRegistry) -> FakeRegistry:
    for data in (ZIP_100, ZIP_110, ZIP_BETA):
        registry.add_package(data)
    return registry


def download(registry: FakeRegistry, *argv: object, env: dict[str, str] | None = None):
    return run("download", *argv, transport=registry.transport, env=env if env is not None else {})


def leftovers(directory: Path) -> list[str]:
    return sorted(item.name for item in directory.iterdir())


class TestSuccess:
    def test_the_latest_stable_version_is_downloaded_to_the_working_directory(
        self, stocked: FakeRegistry, workdir: Path
    ):
        result = download(stocked, NAME)

        assert result.code == 0
        assert result.err == ""
        assert leftovers(workdir) == ["dl-skill-1.1.0.zip"]
        assert (workdir / "dl-skill-1.1.0.zip").read_bytes() == ZIP_110
        lines = result.out.splitlines()
        assert lines[0] == "Downloaded dl-skill 1.1.0 to dl-skill-1.1.0.zip"
        fields = parse_fields(lines[1:])
        assert fields["sha256"] == sha256(ZIP_110)
        assert fields["size"].endswith(f"({len(ZIP_110)} bytes)")
        request = stocked.last_request()
        assert request.url.path == "/api/v1/skills/dl-skill/versions/latest/download"

    def test_an_exact_version_is_requested_and_saved(self, stocked: FakeRegistry, workdir: Path):
        result = download(stocked, f"{NAME}@1.0.0")

        assert result.code == 0
        assert (workdir / "dl-skill-1.0.0.zip").read_bytes() == ZIP_100
        assert stocked.last_request().url.path.endswith("/versions/1.0.0/download")

    def test_a_prerelease_version_can_be_pinned(self, stocked: FakeRegistry, workdir: Path):
        result = download(stocked, f"{NAME}@2.0.0-beta.1")

        assert result.code == 0
        assert (workdir / "dl-skill-2.0.0-beta.1.zip").read_bytes() == ZIP_BETA

    def test_latest_can_be_spelled_out(self, stocked: FakeRegistry, workdir: Path):
        assert download(stocked, f"{NAME}@latest").code == 0
        assert (workdir / "dl-skill-1.1.0.zip").is_file()

    def test_the_output_option_names_the_file(self, stocked: FakeRegistry, tmp_path: Path):
        target = tmp_path / "somewhere" / "copy.zip"

        result = download(stocked, NAME, "-o", target)

        assert result.code == 0
        assert target.read_bytes() == ZIP_110
        assert result.out.splitlines()[0] == f"Downloaded dl-skill 1.1.0 to {target}"

    def test_the_output_option_accepts_an_existing_directory(
        self, stocked: FakeRegistry, tmp_path: Path
    ):
        result = download(stocked, NAME, "--output", tmp_path)

        assert result.code == 0
        assert (tmp_path / "dl-skill-1.1.0.zip").read_bytes() == ZIP_110

    def test_a_trailing_slash_creates_the_directory(self, stocked: FakeRegistry, tmp_path: Path):
        result = download(stocked, NAME, "-o", f"{tmp_path}/new/dir/")

        assert result.code == 0
        assert (tmp_path / "new" / "dir" / "dl-skill-1.1.0.zip").read_bytes() == ZIP_110

    def test_an_existing_file_is_replaced(self, stocked: FakeRegistry, tmp_path: Path):
        target = tmp_path / "copy.zip"
        target.write_bytes(b"stale")

        result = download(stocked, NAME, "-o", target)

        assert result.code == 0
        assert target.read_bytes() == ZIP_110

    def test_the_downloaded_file_is_a_usable_package(self, stocked: FakeRegistry, tmp_path: Path):
        target = tmp_path / "copy.zip"
        download(stocked, NAME, "-o", target)

        assert run("validate", target).code == 0

    def test_json_output(self, stocked: FakeRegistry, workdir: Path):
        result = download(stocked, f"{NAME}@1.0.0", "--json")

        assert result.code == 0
        assert result.err == ""
        assert result.json() == {
            "name": "dl-skill",
            "version": "1.0.0",
            "path": "dl-skill-1.0.0.zip",
            "size_bytes": len(ZIP_100),
            "sha256": sha256(ZIP_100),
        }

    def test_the_resolved_version_is_reported_for_latest_in_json(
        self, stocked: FakeRegistry, workdir: Path
    ):
        assert download(stocked, NAME, "--json").json()["version"] == "1.1.0"

    def test_no_temporary_files_remain_after_a_success(self, stocked: FakeRegistry, tmp_path: Path):
        destination = tmp_path / "dest"

        download(stocked, NAME, "-o", destination / "x.zip")

        assert leftovers(destination) == ["x.zip"]

    def test_a_package_can_be_requested_by_id(self, stocked: FakeRegistry, workdir: Path):
        skill_id = stocked.entries[NAME].skill_id

        result = download(stocked, skill_id)

        assert result.code == 0
        assert (workdir / f"{NAME}-1.1.0.zip").read_bytes() == ZIP_110
        assert result.out.splitlines()[0] == f"Downloaded {skill_id} 1.1.0 to {NAME}-1.1.0.zip"

    def test_a_token_is_sent_for_private_registries(self, stocked: FakeRegistry, workdir: Path):
        stocked.public_read = False

        denied = download(stocked, NAME)
        allowed = download(stocked, NAME, env=remote_env("alx_read"))

        assert denied.code == 1
        assert "UNAUTHORIZED" in denied.err
        assert allowed.code == 0


class TestFailures:
    def test_an_unknown_package_is_not_found(self, stocked: FakeRegistry, workdir: Path):
        result = download(stocked, "no-such-package")

        assert result.code == 1
        assert result.err.startswith("error: NOT_FOUND: ")
        assert leftovers(workdir) == []

    def test_an_unknown_version_is_not_found(self, stocked: FakeRegistry, workdir: Path):
        result = download(stocked, f"{NAME}@9.9.9")

        assert result.code == 1
        assert result.err.startswith("error: NOT_FOUND: ")
        assert leftovers(workdir) == []

    def test_json_mode_reports_errors_as_json_on_stderr(self, stocked: FakeRegistry, workdir: Path):
        result = download(stocked, "no-such-package", "--json")

        assert result.code == 1
        assert result.out == ""
        assert result.error_json()["error"]["code"] == "NOT_FOUND"

    def test_other_success_statuses_are_not_accepted(self, stocked: FakeRegistry, workdir: Path):
        stocked.raw_response = httpx.Response(204)

        result = download(stocked, NAME)

        assert result.code == 1
        assert result.err == "error: HTTP_204: unexpected response from the server (HTTP 204)\n"

    def test_a_redirect_is_not_followed(self, stocked: FakeRegistry, workdir: Path):
        stocked.redirect_to = "https://elsewhere.example.org/file.zip"

        result = download(stocked, NAME)

        assert result.code == 1
        assert "redirect (HTTP 301)" in result.err
        assert len(stocked.requests) == 1
        assert leftovers(workdir) == []

    def test_a_network_error_before_the_body(self, stocked: FakeRegistry, workdir: Path):
        stocked.failure = httpx.ConnectError("refused")

        result = download(stocked, NAME)

        assert result.code == 1
        assert result.err.startswith("error: NETWORK_ERROR: cannot connect to")
        assert leftovers(workdir) == []

    def test_a_connection_lost_in_the_middle_of_the_body_leaves_nothing(
        self, stocked: FakeRegistry, workdir: Path
    ):
        class Broken(httpx.SyncByteStream):
            def __iter__(self):
                yield ZIP_110[:50]
                raise httpx.ReadError("connection lost")

        stocked.raw_response = httpx.Response(
            200, headers={"X-Content-SHA256": sha256(ZIP_110)}, stream=Broken()
        )

        result = download(stocked, NAME)

        assert result.code == 1
        assert result.err.startswith("error: NETWORK_ERROR: ")
        assert "Traceback" not in result.err
        assert leftovers(workdir) == []

    @pytest.mark.parametrize("target", ["file", "dir"])
    def test_a_failed_download_creates_nothing_and_keeps_an_existing_target(
        self, stocked: FakeRegistry, tmp_path: Path, target: str
    ):
        stocked.serve(NAME, "1.0.0", ZIP_100, sha256="0" * 64)
        existing = tmp_path / "keep.zip"
        existing.write_bytes(b"precious")
        output = existing if target == "file" else tmp_path / "fresh" / "nested" / "x.zip"

        result = download(stocked, f"{NAME}@1.0.0", "-o", output)

        assert result.code == 1
        assert existing.read_bytes() == b"precious"
        assert not (tmp_path / "fresh").exists()
        assert leftovers(tmp_path) == ["keep.zip"]

    def test_a_directory_in_the_way_of_the_final_file_is_reported(
        self, stocked: FakeRegistry, tmp_path: Path
    ):
        (tmp_path / "dl-skill-1.1.0.zip").mkdir()

        result = download(stocked, NAME, "-o", tmp_path)

        assert result.code == 1
        assert result.err.startswith("error: Is a directory")
        assert "Traceback" not in result.err
        assert leftovers(tmp_path) == ["dl-skill-1.1.0.zip"]

    def test_an_existing_directory_given_as_the_output_receives_the_file(
        self, stocked: FakeRegistry, tmp_path: Path
    ):
        (tmp_path / "taken.zip").mkdir()

        result = download(stocked, NAME, "-o", tmp_path / "taken.zip")

        assert result.code == 0
        assert (tmp_path / "taken.zip" / "dl-skill-1.1.0.zip").is_file()


class TestIntegrity:
    def test_a_wrong_sha256_header_aborts_the_download(self, stocked: FakeRegistry, workdir: Path):
        stocked.serve(NAME, "1.0.0", ZIP_100, sha256=sha256(b"something else"))

        result = download(stocked, f"{NAME}@1.0.0")

        assert result.code == 1
        assert result.err.startswith("error: INTEGRITY_ERROR: sha256 mismatch")
        assert sha256(b"something else") in result.err
        assert sha256(ZIP_100) in result.err
        assert leftovers(workdir) == []

    def test_a_tampered_body_is_detected(self, stocked: FakeRegistry, workdir: Path):
        tampered = bytearray(ZIP_100)
        tampered[40] ^= 0xFF
        stocked.serve(NAME, "1.0.0", bytes(tampered), sha256=sha256(ZIP_100))

        result = download(stocked, f"{NAME}@1.0.0")

        assert result.code == 1
        assert "sha256 mismatch" in result.err
        assert leftovers(workdir) == []

    def test_a_truncated_body_is_detected(self, stocked: FakeRegistry, workdir: Path):
        stocked.serve(NAME, "1.0.0", ZIP_100[:-10], sha256=sha256(ZIP_100))

        result = download(stocked, f"{NAME}@1.0.0")

        assert result.code == 1
        assert "sha256 mismatch" in result.err
        assert leftovers(workdir) == []

    @pytest.mark.parametrize(
        "header", [None, "", "abc", "z" * 64, "0" * 63, "0" * 65, " ", sha256(ZIP_100) + "ff"]
    )
    def test_a_missing_or_malformed_hash_header_is_refused(
        self, stocked: FakeRegistry, workdir: Path, header: str | None
    ):
        stocked.serve(NAME, "1.0.0", ZIP_100, sha256=None, headers=_header(header))

        result = download(stocked, f"{NAME}@1.0.0")

        assert result.code == 1
        assert "X-Content-SHA256" in result.err
        assert result.err.startswith("error: INTEGRITY_ERROR: ")
        assert leftovers(workdir) == []

    def test_an_uppercase_hash_is_accepted(self, stocked: FakeRegistry, workdir: Path):
        stocked.serve(NAME, "1.0.0", ZIP_100, sha256=sha256(ZIP_100).upper())

        result = download(stocked, f"{NAME}@1.0.0")

        assert result.code == 0
        assert (workdir / "dl-skill-1.0.0.zip").read_bytes() == ZIP_100

    def test_the_etag_alone_is_not_enough(self, stocked: FakeRegistry, workdir: Path):
        stocked.serve(NAME, "1.0.0", ZIP_100, sha256=None, headers={"ETag": f'"{sha256(ZIP_100)}"'})

        assert download(stocked, f"{NAME}@1.0.0").code == 1
        assert leftovers(workdir) == []

    def test_a_non_zip_body_with_a_matching_hash_is_still_just_a_download(
        self, stocked: FakeRegistry, workdir: Path
    ):
        stocked.serve(NAME, "1.0.0", b"just bytes")

        result = download(stocked, f"{NAME}@1.0.0")

        assert result.code == 0
        assert (workdir / "dl-skill-1.0.0.zip").read_bytes() == b"just bytes"


def _header(value: str | None) -> dict[str, str]:
    return {} if value is None else {"X-Content-SHA256": value}


class TestSizeLimit:
    @pytest.fixture(autouse=True)
    def small_limit(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(cli, "DEFAULT_LIMITS", PackageLimits(max_package_bytes=500))

    def test_an_announced_length_above_the_limit_is_refused_before_reading(
        self, registry: FakeRegistry, workdir: Path
    ):
        data = make_skill_zip(NAME, "1.0.0", files={"big.bin": bytes(range(256)) * 20})
        registry.serve(NAME, "1.0.0", data)
        assert len(data) > 500

        result = download(registry, f"{NAME}@1.0.0")

        assert result.code == 1
        assert result.err.startswith("error: TOO_LARGE: the server announces")
        assert leftovers(workdir) == []

    def test_a_body_that_outgrows_its_announced_length_is_cut_off(
        self, registry: FakeRegistry, workdir: Path
    ):
        data = make_skill_zip(NAME, "1.0.0", files={"big.bin": bytes(range(256)) * 20})
        registry.serve(NAME, "1.0.0", data, headers={"Content-Length": "10"})

        result = download(registry, f"{NAME}@1.0.0")

        assert result.code == 1
        assert result.err.startswith("error: TOO_LARGE: the download exceeds")
        assert leftovers(workdir) == []

    def test_a_package_within_the_limit_is_fine(self, registry: FakeRegistry, workdir: Path):
        registry.add_package(make_skill_zip(NAME, "1.0.0"))

        assert download(registry, NAME).code == 0


class TestSuggestedFilename:
    @pytest.mark.parametrize(
        "disposition",
        [
            'attachment; filename="../../evil.zip"',
            'attachment; filename="/etc/cron.d/evil.zip"',
            'attachment; filename="a\\b.zip"',
            'attachment; filename="evil"',
            'attachment; filename="..zip"',
            'attachment; filename=""',
            "attachment; filename=unquoted.zip",
            "attachment; filename*=UTF-8''..%2Fevil.zip",
            'attachment; filename="x/y.zip"',
            'attachment; filename="line\nbreak.zip"',
            "inline",
        ],
    )
    def test_a_hostile_filename_from_the_server_is_never_used(
        self, registry: FakeRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, disposition
    ):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.chdir(home)
        registry.serve(NAME, "1.0.0", ZIP_100, headers={"Content-Disposition": disposition})

        result = download(registry, f"{NAME}@1.0.0")

        assert result.code == 0
        assert leftovers(home) == ["dl-skill-1.0.0.zip"]
        assert leftovers(tmp_path) == ["home"]
        assert not Path("/etc/cron.d/evil.zip").exists()

    def test_a_clean_filename_names_the_file_and_gives_the_resolved_version(
        self, registry: FakeRegistry, workdir: Path
    ):
        registry.serve(
            NAME,
            "latest",
            ZIP_110,
            headers={"Content-Disposition": f'attachment; filename="{NAME}-1.1.0.zip"'},
        )

        result = download(registry, NAME)

        assert result.code == 0
        assert leftovers(workdir) == ["dl-skill-1.1.0.zip"]
        assert result.out.splitlines()[0] == "Downloaded dl-skill 1.1.0 to dl-skill-1.1.0.zip"

    def test_a_filename_for_another_package_does_not_change_the_reported_version(
        self, registry: FakeRegistry, workdir: Path
    ):
        registry.serve(
            NAME,
            "latest",
            ZIP_110,
            headers={"Content-Disposition": 'attachment; filename="other-9.9.9.zip"'},
        )

        result = download(registry, NAME)

        assert leftovers(workdir) == ["other-9.9.9.zip"]
        assert result.out.splitlines()[0].startswith("Downloaded dl-skill latest ")

    def test_without_a_filename_the_requested_version_names_the_file(
        self, registry: FakeRegistry, workdir: Path
    ):
        registry.serve(NAME, "latest", ZIP_110)

        result = download(registry, NAME)

        assert leftovers(workdir) == ["dl-skill-latest.zip"]
        assert result.out.splitlines()[0] == "Downloaded dl-skill latest to dl-skill-latest.zip"


class TestHostileErrorBodies:
    def test_an_endless_error_body_is_refused(
        self, stocked: FakeRegistry, workdir: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(cli, "MAX_RESPONSE_BYTES", 200)
        stocked.raw_response = httpx.Response(404, content=b"x" * 500)

        result = download(stocked, NAME)

        assert result.code == 1
        assert result.err.startswith("error: TOO_LARGE: the server sent more than 200 bytes")
        assert leftovers(workdir) == []


class TestReferences:
    @pytest.mark.parametrize(
        "reference",
        [
            "",
            "@1.0.0",
            "dl-skill@",
            "dl-skill@1.0",
            "dl-skill@v1.0.0",
            "dl-skill@1.0.0+build",
            "dl-skill@1.0.0@2",
            "Dl-Skill",
            "dl_skill",
            "dl skill",
            "dl-skill/../x",
            "../etc/passwd",
            "-leading",
            "trailing-",
            "a" * 65,
        ],
    )
    def test_invalid_references_are_usage_errors(self, stocked: FakeRegistry, reference: str):
        result = download(stocked, reference)

        assert result.code == 2
        assert result.err.splitlines()[-1].startswith("alx download: error: ")
        assert stocked.requests == []

    def test_uuid_references_are_accepted(self, stocked: FakeRegistry, workdir: Path):
        result = download(stocked, str(uuid.uuid4()))

        assert result.code == 1
        assert result.err.startswith("error: NOT_FOUND: ")
        assert len(stocked.requests) == 1

    def test_a_reference_is_percent_encoded_in_the_path(self, stocked: FakeRegistry, workdir: Path):
        download(stocked, f"{NAME}@1.0.0")

        assert stocked.last_request().url.raw_path == (
            b"/api/v1/skills/dl-skill/versions/1.0.0/download"
        )

    def test_the_reference_is_required(self):
        assert run("download").code == 2
