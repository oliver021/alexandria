from __future__ import annotations

import hashlib
import random
from pathlib import Path

import httpx
import pytest

from app.cli import pack_directory
from app.config.settings import settings
from tests.helpers.packages import make_grounding_zip
from tests.test_cli.live import Live
from tests.test_cli.support import LOCAL_URL, parse_fields, skill_tree, tree_snapshot

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "packages"
EXAMPLE_NAMES = ["changelog-from-commits", "http-reference", "incident-postmortem"]


def rest(live: Live, token: str | None = None) -> httpx.Client:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.Client(transport=live.transport, base_url=LOCAL_URL, headers=headers)


def version_tree(tmp_path: Path, version: str, name: str = "live-skill", **frontmatter) -> Path:
    return skill_tree(tmp_path / f"{name}-{version}", name, version, **frontmatter)


@pytest.fixture
def writer(live: Live) -> str:
    return live.token("alice")


class TestPublishing:
    def test_a_new_entry_starts_as_a_draft_that_only_its_owner_sees(
        self, live: Live, writer: str, tmp_path: Path
    ):
        source = version_tree(tmp_path, "1.0.0", description="A live skill.")

        published = live.run("publish", source, token=writer)

        assert published.code == 0, published.err
        fields = parse_fields(published.out.splitlines()[1:])
        assert published.out.splitlines()[0] == "Uploaded live-skill 1.0.0 (skill)"
        assert fields["status"] == "draft"
        assert fields["sha256"] == pack_directory(source).parsed.sha256
        assert live.run("search", "--json").json()["total"] == 0
        assert live.run("download", "live-skill", env={}).code == 1
        owner_view = live.run("search", "--json", token=writer).json()
        assert [item["name"] for item in owner_view["items"]] == ["live-skill"]
        assert owner_view["items"][0]["status"] == "draft"

    def test_publishing_in_the_same_request_makes_it_public(
        self, live: Live, writer: str, tmp_path: Path
    ):
        source = version_tree(tmp_path, "1.0.0")

        published = live.run("publish", source, "--publish", token=writer)

        assert published.code == 0, published.err
        assert parse_fields(published.out.splitlines()[1:])["status"] == "published"
        found = live.run("search", "--json").json()
        assert [item["name"] for item in found["items"]] == ["live-skill"]
        assert found["items"][0]["latest_version"] == "1.0.0"

    def test_the_json_output_is_the_servers_upload_result(
        self, live: Live, writer: str, tmp_path: Path
    ):
        source = version_tree(tmp_path, "1.0.0", tags=["one", "two"], category="demos")

        result = live.run("publish", source, "--publish", "--json", token=writer)

        payload = result.json()
        assert payload["skill"]["name"] == "live-skill"
        assert payload["skill"]["kind"] == "skill"
        assert payload["skill"]["tags"] == ["one", "two"]
        assert payload["skill"]["category"] == "demos"
        assert payload["version"]["version"] == "1.0.0"
        assert payload["version"]["status"] == "published"
        assert payload["version"]["sha256"] == pack_directory(source).parsed.sha256
        assert payload["version"]["size_bytes"] == len(pack_directory(source).data)

    def test_the_version_option_travels_with_the_upload(
        self, live: Live, writer: str, tmp_path: Path
    ):
        source = version_tree(tmp_path, "3.1.4")

        result = live.run("publish", source, "--version", "3.1.4", "--publish", token=writer)

        assert result.code == 0, result.err
        assert result.out.startswith("Uploaded live-skill 3.1.4")

    def test_a_package_without_a_frontmatter_version_takes_it_from_the_option(
        self, live: Live, writer: str, tmp_path: Path
    ):
        source = skill_tree(tmp_path / "pkg", "late-version", None)

        result = live.run("publish", source, "--version", "0.4.0", "--publish", token=writer)

        assert result.code == 0, result.err
        assert result.out.startswith("Uploaded late-version 0.4.0")

    def test_a_zip_made_elsewhere_is_published_as_it_is(
        self, live: Live, writer: str, tmp_path: Path
    ):
        archive = tmp_path / "external.zip"
        data = make_grounding_zip(
            "external-facts", "1.0.0", wrapper="external", files={"data/x.csv": "a,b\n1,2\n"}
        )
        archive.write_bytes(data)

        published = live.run("publish", archive, "--publish", token=writer)
        pulled = live.run("pull", "external-facts", "-d", tmp_path / "out")

        assert published.code == 0, published.err
        assert pulled.code == 0, pulled.err
        assert sorted(tree_snapshot(tmp_path / "out")) == ["INDEX.md", "data/x.csv"]
        downloaded = live.run("download", "external-facts", "-o", tmp_path / "back.zip")
        assert downloaded.code == 0
        assert (tmp_path / "back.zip").read_bytes() == data

    def test_a_jwt_is_as_good_as_an_api_token(self, live: Live, tmp_path: Path):
        token = live.jwt("carol")
        source = version_tree(tmp_path, "1.0.0")

        result = live.run("publish", source, "--publish", token=token)

        assert result.code == 0, result.err

    def test_each_new_version_updates_the_entry(self, live: Live, writer: str, tmp_path: Path):
        live.run("publish", version_tree(tmp_path, "1.0.0"), "--publish", token=writer)
        live.run(
            "publish",
            version_tree(tmp_path, "1.1.0", description="Second description."),
            "--publish",
            token=writer,
        )

        found = live.run("search", "--json").json()["items"][0]

        assert found["latest_version"] == "1.1.0"
        assert found["description"] == "Second description."


class TestRejections:
    def test_the_same_version_cannot_be_uploaded_twice(
        self, live: Live, writer: str, tmp_path: Path
    ):
        source = version_tree(tmp_path, "1.0.0")
        live.run("publish", source, token=writer)

        second = live.run("publish", source, token=writer)

        assert second.code == 1
        assert second.err.startswith("error: CONFLICT: ")
        assert "1.0.0" in second.err

    def test_an_entry_cannot_change_its_kind(self, live: Live, writer: str, tmp_path: Path):
        live.run("publish", version_tree(tmp_path, "1.0.0", "shared-name"), token=writer)
        grounding = tmp_path / "grounding.zip"
        grounding.write_bytes(make_grounding_zip("shared-name", "2.0.0"))

        result = live.run("publish", grounding, token=writer)

        assert result.code == 1
        assert result.err.startswith("error: CONFLICT: ")

    def test_another_user_cannot_add_versions_to_an_entry(
        self, live: Live, writer: str, tmp_path: Path
    ):
        live.run("publish", version_tree(tmp_path, "1.0.0"), "--publish", token=writer)
        intruder = live.token("mallory")

        result = live.run("publish", version_tree(tmp_path, "1.0.1"), token=intruder)

        assert result.code == 1
        assert result.err.startswith("error: FORBIDDEN: ")

    def test_a_read_only_token_cannot_publish(self, live: Live, tmp_path: Path):
        token = live.token("alice", scope="read")

        result = live.run("publish", version_tree(tmp_path, "1.0.0"), token=token)

        assert result.code == 1
        assert result.err.startswith("error: FORBIDDEN: ")

    def test_an_unknown_token_is_unauthorized_with_a_hint(self, live: Live, tmp_path: Path):
        result = live.run("publish", version_tree(tmp_path, "1.0.0"), token="alx_not-a-real-token")

        assert result.code == 1
        assert result.err.startswith("error: UNAUTHORIZED: ")
        assert "check that the token is valid" in result.err
        assert "alx_not-a-real-token" not in result.err

    def test_a_revoked_token_stops_working(self, live: Live, tmp_path: Path):
        token = live.token("alice")
        with rest(live, token) as client:
            tokens = client.get("/api/v1/tokens").json()
            assert client.delete(f"/api/v1/tokens/{tokens[0]['id']}").status_code == 204

        result = live.run("publish", version_tree(tmp_path, "1.0.0"), token=token)

        assert result.code == 1
        assert result.err.startswith("error: UNAUTHORIZED: ")

    def test_the_server_enforces_its_own_limits_with_a_reason(
        self, live: Live, writer: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(settings.registry.limits, "max_files", 2)

        result = live.run("publish", version_tree(tmp_path, "1.0.0"), token=writer)

        assert result.code == 1
        assert result.err.startswith("error: INVALID_PACKAGE (TOO_MANY_FILES): ")

    def test_a_package_above_the_servers_size_limit_is_payload_too_large(
        self, live: Live, writer: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        source = version_tree(
            tmp_path, "1.0.0", files={"noise.bin": random.Random(7).randbytes(6000)}
        )
        monkeypatch.setattr(settings.registry.limits, "max_package_bytes", 2000)

        result = live.run("publish", source, token=writer)

        assert result.code == 1
        assert result.err.startswith("error: PAYLOAD_TOO_LARGE: ")

    def test_the_json_error_of_the_server_is_passed_on(
        self, live: Live, writer: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(settings.registry.limits, "max_files", 2)

        result = live.run("publish", version_tree(tmp_path, "1.0.0"), "--json", token=writer)

        error = result.error_json()["error"]
        assert (error["code"], error["reason"]) == ("INVALID_PACKAGE", "TOO_MANY_FILES")


class TestSearching:
    @pytest.fixture
    def catalog(self, live: Live, writer: str, tmp_path: Path) -> Live:
        live.run(
            "publish",
            version_tree(
                tmp_path,
                "1.0.0",
                "git-helper",
                description="Helps with git history.",
                tags=["git", "history"],
            ),
            "--publish",
            token=writer,
        )
        live.run(
            "publish",
            version_tree(
                tmp_path,
                "2.0.0",
                "csv-tools",
                description="Work with CSV files.",
                tags=["csv", "data"],
            ),
            "--publish",
            token=writer,
        )
        archive = tmp_path / "facts.zip"
        archive.write_bytes(
            make_grounding_zip(
                "city-facts", "1.0.0", description="Facts about cities.", tags=["data", "cities"]
            )
        )
        live.run("publish", archive, "--publish", token=writer)
        return live

    def names(self, result) -> list[str]:
        return [item["name"] for item in result.json()["items"]]

    def test_everything_is_listed_by_name(self, catalog: Live):
        result = catalog.run("search")

        assert result.code == 0
        assert [line.split()[0] for line in result.out.split("\n\n")[0].splitlines()[1:]] == [
            "city-facts",
            "csv-tools",
            "git-helper",
        ]

    def test_text_and_filters_reach_the_real_search(self, catalog: Live):
        assert self.names(catalog.run("search", "git", "--json")) == ["git-helper"]
        assert self.names(catalog.run("search", "--kind", "grounding", "--json")) == ["city-facts"]
        assert self.names(catalog.run("search", "--tag", "data", "--json")) == [
            "city-facts",
            "csv-tools",
        ]
        assert self.names(catalog.run("search", "--tag", "data", "--tag", "csv", "--json")) == [
            "csv-tools"
        ]
        assert self.names(catalog.run("search", "--tag", "DATA", "--tag", "csv", "--json")) == [
            "csv-tools"
        ]
        assert self.names(catalog.run("search", "nothing-like-this", "--json")) == []

    def test_pagination_walks_the_catalog(self, catalog: Live):
        first = catalog.run("search", "--limit", "2")
        second = catalog.run("search", "--limit", "2", "--offset", "2")

        assert "More results are available: use --offset 2" in first.out
        assert "2 of 3 result(s) shown" in first.out
        assert "1 of 3 result(s) shown" in second.out
        assert "More results" not in second.out

    def test_the_server_rejects_what_the_client_did_not_catch(self, catalog: Live):
        result = catalog.run("search", "--json", "--url", "http://localhost:8000/", "x" * 600)

        assert result.code == 1
        assert result.error_json()["error"]["code"] == "VALIDATION_ERROR"


class TestConsuming:
    @pytest.fixture
    def published(self, live: Live, writer: str) -> dict[str, bytes]:
        archives = {}
        for name in EXAMPLE_NAMES:
            result = live.run("publish", EXAMPLES / name, "--publish", token=writer)
            assert result.code == 0, result.err
            archives[name] = pack_directory(EXAMPLES / name).data
        return archives

    def test_the_server_returns_the_exact_bytes_that_were_packed(
        self, live: Live, published: dict[str, bytes], tmp_path: Path
    ):
        for name, data in published.items():
            result = live.run("download", name, "-o", tmp_path / f"{name}.zip", "--json")

            assert result.code == 0, result.err
            report = result.json()
            assert (tmp_path / f"{name}.zip").read_bytes() == data
            assert report["sha256"] == hashlib.sha256(data).hexdigest()
            assert report["size_bytes"] == len(data)
            assert report["version"] == "1.0.0"

    def test_the_default_file_name_comes_from_the_server(
        self, live: Live, published: dict[str, bytes], tmp_path: Path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)

        result = live.run("download", "http-reference")

        assert result.code == 0, result.err
        assert (tmp_path / "http-reference-1.0.0.zip").is_file()
        assert result.out.splitlines()[0] == (
            "Downloaded http-reference 1.0.0 to http-reference-1.0.0.zip"
        )

    def test_pulling_reproduces_every_example_byte_for_byte(
        self, live: Live, published: dict[str, bytes], tmp_path: Path
    ):
        for name in published:
            result = live.run("pull", name, "-d", tmp_path / name)

            assert result.code == 0, result.err
            expected = {
                path: (content, 0o755 if mode & 0o100 else 0o644)
                for path, (content, mode) in tree_snapshot(EXAMPLES / name).items()
            }
            assert tree_snapshot(tmp_path / name) == expected

    def test_pulling_a_fixed_version_ignores_later_ones(
        self, live: Live, writer: str, tmp_path: Path
    ):
        for version, text in (("1.0.0", "first"), ("1.1.0", "second")):
            source = version_tree(tmp_path, version, files={"marker.txt": text})
            live.run("publish", source, "--publish", token=writer)

        old = live.run("pull", "live-skill@1.0.0", "-d", tmp_path / "old")
        new = live.run("pull", "live-skill", "-d", tmp_path / "new")

        assert (old.code, new.code) == (0, 0)
        assert (tmp_path / "old" / "marker.txt").read_text() == "first"
        assert (tmp_path / "new" / "marker.txt").read_text() == "second"

    def test_latest_prefers_stable_releases_and_skips_drafts(
        self, live: Live, writer: str, tmp_path: Path
    ):
        for version, publish in (("1.0.0", True), ("2.0.0-rc.1", True), ("3.0.0", False)):
            args = ["--publish"] if publish else []
            live.run("publish", version_tree(tmp_path, version), *args, token=writer)

        result = live.run("pull", "live-skill", "-d", tmp_path / "out", "--json")

        assert result.json()["version"] == "1.0.0"

    def test_a_draft_can_be_pulled_by_its_owner_only(self, live: Live, writer: str, tmp_path: Path):
        live.run("publish", version_tree(tmp_path, "1.0.0"), token=writer)

        anonymous = live.run("pull", "live-skill@1.0.0", "-d", tmp_path / "anon")
        owner = live.run("pull", "live-skill@1.0.0", "-d", tmp_path / "owner", token=writer)
        stranger = live.run(
            "pull", "live-skill@1.0.0", "-d", tmp_path / "other", token=live.token("bob")
        )

        assert anonymous.code == 1 and anonymous.err.startswith("error: NOT_FOUND: ")
        assert owner.code == 0, owner.err
        assert stranger.code == 1 and stranger.err.startswith("error: NOT_FOUND: ")
        assert not (tmp_path / "anon").exists() and not (tmp_path / "other").exists()

    def test_a_deprecated_version_is_still_pullable_by_number(
        self, live: Live, writer: str, tmp_path: Path
    ):
        for version in ("1.0.0", "1.1.0"):
            live.run("publish", version_tree(tmp_path, version), "--publish", token=writer)
        with rest(live, writer) as client:
            assert (
                client.post("/api/v1/skills/live-skill/versions/1.1.0/deprecate").status_code == 200
            )

        exact = live.run("pull", "live-skill@1.1.0", "-d", tmp_path / "exact", "--json")
        latest = live.run("pull", "live-skill", "-d", tmp_path / "latest", "--json")

        assert exact.json()["version"] == "1.1.0"
        assert latest.json()["version"] == "1.0.0"

    def test_hidden_entries_disappear_and_come_back(self, live: Live, writer: str, tmp_path: Path):
        live.run("publish", version_tree(tmp_path, "1.0.0"), "--publish", token=writer)
        with rest(live, writer) as client:
            assert client.delete("/api/v1/skills/live-skill").status_code == 204

        hidden = live.run("pull", "live-skill", "-d", tmp_path / "a")
        with rest(live, writer) as client:
            assert client.post("/api/v1/skills/live-skill/restore").status_code == 200
        restored = live.run("pull", "live-skill", "-d", tmp_path / "b")

        assert hidden.code == 1 and hidden.err.startswith("error: NOT_FOUND: ")
        assert restored.code == 0

    def test_an_unknown_package_is_not_found(self, live: Live, tmp_path: Path):
        result = live.run("pull", "nothing-here", "-d", tmp_path / "out")

        assert result.code == 1
        assert result.err.startswith("error: NOT_FOUND: ")

    def test_pulling_by_uuid_works(self, live: Live, writer: str, tmp_path: Path):
        created = live.run(
            "publish", version_tree(tmp_path, "1.0.0"), "--publish", "--json", token=writer
        ).json()

        result = live.run("pull", created["skill"]["id"], "-d", tmp_path / "out")

        assert result.code == 0, result.err
        assert (tmp_path / "out" / "SKILL.md").is_file()


class TestPrivateRegistry:
    @pytest.fixture
    def private(self, live: Live, writer: str, tmp_path: Path, monkeypatch) -> Live:
        live.run("publish", version_tree(tmp_path, "1.0.0"), "--publish", token=writer)
        monkeypatch.setattr(settings.registry, "public_read", False)
        return live

    def test_reading_without_a_credential_is_refused_with_a_hint(self, private: Live, tmp_path):
        for argv in (
            ("search",),
            ("download", "live-skill", "-o", tmp_path / "x.zip"),
            ("pull", "live-skill", "-d", tmp_path / "x"),
        ):
            result = private.run(*argv)

            assert result.code == 1, argv
            assert result.err.startswith("error: UNAUTHORIZED: ")
            assert "set ALX_TOKEN" in result.err

    def test_a_read_token_is_enough_to_consume(self, private: Live, tmp_path):
        reader = private.token("reader", scope="read")

        searched = private.run("search", token=reader)
        pulled = private.run("pull", "live-skill", "-d", tmp_path / "out", token=reader)

        assert searched.code == 0 and "live-skill" in searched.out
        assert pulled.code == 0, pulled.err
        assert (tmp_path / "out" / "SKILL.md").is_file()
