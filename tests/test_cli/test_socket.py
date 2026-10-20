from __future__ import annotations

import hashlib
import random
from pathlib import Path

import pytest

from app.cli import pack_directory
from app.config.settings import settings
from tests.test_cli.live import Served
from tests.test_cli.support import parse_fields, skill_tree, tree_snapshot

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "packages"


@pytest.fixture
def writer(served: Served) -> str:
    return served.live.token("alice")


class TestOverRealHttp:
    def test_the_whole_cycle_works_over_a_real_connection(
        self, served: Served, writer: str, tmp_path: Path
    ):
        source = EXAMPLES / "http-reference"

        published = served.run("publish", source, "--publish", token=writer)
        found = served.run("search", "http", "--json")
        downloaded = served.run("download", "http-reference", "-o", tmp_path / "back.zip")
        pulled = served.run("pull", "http-reference", "-d", tmp_path / "out")

        assert published.code == 0, published.err
        assert [item["name"] for item in found.json()["items"]] == ["http-reference"]
        assert downloaded.code == 0, downloaded.err
        assert (tmp_path / "back.zip").read_bytes() == pack_directory(source).data
        assert pulled.code == 0, pulled.err
        expected = {
            path: (content, 0o755 if mode & 0o100 else 0o644)
            for path, (content, mode) in tree_snapshot(source).items()
        }
        assert tree_snapshot(tmp_path / "out") == expected

    def test_a_large_incompressible_package_is_streamed_both_ways(
        self, served: Served, writer: str, tmp_path: Path
    ):
        payload = random.Random(11).randbytes(6 * 1024 * 1024)
        source = skill_tree(tmp_path / "big", "big-skill", "1.0.0", files={"noise.bin": payload})
        packed = pack_directory(source)
        assert len(packed.data) > 5 * 1024 * 1024

        published = served.run("publish", source, "--publish", token=writer)
        pulled = served.run("pull", "big-skill", "-d", tmp_path / "out")

        assert published.code == 0, published.err
        assert parse_fields(published.out.splitlines()[1:])["sha256"] == packed.parsed.sha256
        assert pulled.code == 0, pulled.err
        assert hashlib.sha256((tmp_path / "out" / "noise.bin").read_bytes()).hexdigest() == (
            hashlib.sha256(payload).hexdigest()
        )

    def test_server_errors_are_decoded_from_the_wire(
        self, served: Served, writer: str, tmp_path: Path
    ):
        source = skill_tree(tmp_path / "pkg", "wire-skill", "1.0.0")
        first = served.run("publish", source, token=writer)
        again = served.run("publish", source, token=writer)
        wrong = served.run("publish", source, token="alx_nope")
        missing = served.run("pull", "no-such-package", "-d", tmp_path / "x")

        assert first.code == 0, first.err
        assert again.err.startswith("error: CONFLICT: ")
        assert wrong.err.startswith("error: UNAUTHORIZED: ")
        assert missing.err.startswith("error: NOT_FOUND: ")

    def test_a_body_beyond_the_servers_limit_is_reported_cleanly(
        self, served: Served, writer: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(settings.registry.limits, "max_package_bytes", 1000)
        payload = random.Random(5).randbytes(400 * 1024)
        source = skill_tree(tmp_path / "big", "too-big", "1.0.0", files={"noise.bin": payload})

        result = served.run("publish", source, "--json", token=writer)

        assert result.code == 1
        assert "Traceback" not in result.err
        assert result.error_json()["error"]["code"] in {"PAYLOAD_TOO_LARGE", "NETWORK_ERROR"}

    def test_a_closed_port_is_a_clean_network_error(self, served: Served, tmp_path: Path):
        source = skill_tree(tmp_path / "pkg")

        result = served.run("publish", source, "--url", "http://127.0.0.1:1", token="alx_x")

        assert result.code == 1
        assert result.err.startswith("error: NETWORK_ERROR: cannot connect to")

    def test_the_download_headers_are_what_the_client_relies_on(
        self, served: Served, writer: str, tmp_path: Path, workdir: Path
    ):
        source = skill_tree(tmp_path / "pkg", "header-skill", "1.2.3")
        served.run("publish", source, "--publish", token=writer)

        result = served.run("download", "header-skill", "--json")

        assert result.code == 0, result.err
        report = result.json()
        assert report["version"] == "1.2.3"
        assert report["path"] == "header-skill-1.2.3.zip"
        assert report["sha256"] == pack_directory(source).parsed.sha256
        assert (workdir / report["path"]).is_file()


class TestCredentialsOnTheWire:
    @pytest.mark.parametrize(
        "token",
        ["alx_good\r\nX-Injected: yes", "alx_good\nbad", "alx good", "alx_\x00good", "alx_ñandú"],
    )
    def test_a_token_that_cannot_be_a_header_value_is_rejected_without_echoing_it(
        self, served: Served, tmp_path: Path, token: str
    ):
        source = skill_tree(tmp_path / "pkg")

        result = served.run("publish", source, "--token", token)

        assert result.code == 2
        assert result.err.startswith("alx publish: error: ")
        assert "Injected" not in result.err
        assert "good" not in result.err
