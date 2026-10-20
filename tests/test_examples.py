from __future__ import annotations

import ast
import csv
import hashlib
import html
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tokenize
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

import pytest
import yaml

from app.cli import pack_directory
from app.services.package_inspector import inspect_package
from tests.test_cli.support import FakeRegistry, remote_env, run, tree_snapshot

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
PACKAGES = EXAMPLES / "packages"

REQUIRED_FRONTMATTER = {"name", "display_name", "description", "version", "tags", "category"}
JUNK_NAMES = {".git", "__pycache__", ".DS_Store", "node_modules", "Thumbs.db"}


@dataclass(frozen=True)
class Expected:
    kind: str
    version: str
    entrypoint: str
    files: tuple[str, ...]


EXPECTED = {
    "changelog-from-commits": Expected(
        "skill",
        "1.0.0",
        "SKILL.md",
        (
            "SKILL.md",
            "scripts/collect_commits.sh",
            "scripts/format_changelog.py",
            "references/conventional-commits.md",
            "references/keep-a-changelog.md",
        ),
    ),
    "http-reference": Expected(
        "grounding",
        "1.0.0",
        "INDEX.md",
        (
            "INDEX.md",
            "tables/status_codes.csv",
            "tables/methods.csv",
            "tables/scenarios.csv",
            "spec/problem-details.yaml",
            "site/cheatsheet.html",
            "db/http-reference.sqlite",
            "scripts/build_db.py",
        ),
    ),
    "incident-postmortem": Expected(
        "skill",
        "1.0.0",
        "SKILL.md",
        (
            "SKILL.md",
            "templates/postmortem.html",
            "scripts/render_postmortem.py",
            "references/severity-levels.md",
            "references/template-fields.md",
            "references/example-incident.json",
        ),
    ),
}

NAMES = sorted(EXPECTED)


@pytest.fixture(autouse=True)
def isolated_working_directory(tmp_path_factory, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path_factory.mktemp("cwd"))


def package_dir(name: str) -> Path:
    return PACKAGES / name


def frontmatter(name: str) -> dict:
    entrypoint = package_dir(name) / EXPECTED[name].entrypoint
    text = entrypoint.read_text(encoding="utf-8")
    block = text.split("---\n", 2)[1]
    return yaml.safe_load(block)


def python_scripts() -> list[Path]:
    return sorted(EXAMPLES.rglob("*.py"))


def run_script(
    script: Path, *args: object, stdin: str | None = None, env: dict[str, str] | None = None
):
    return subprocess.run(
        [sys.executable, "-I", "-B", str(script), *[str(item) for item in args]],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


class TestCatalogue:
    def test_every_example_directory_is_registered_in_this_test(self):
        found = sorted(item.name for item in PACKAGES.iterdir())

        assert found == NAMES

    @pytest.mark.parametrize("name", NAMES)
    def test_the_directory_is_named_after_the_package(self, name: str):
        assert frontmatter(name)["name"] == name
        assert package_dir(name).is_dir()

    def test_the_readme_describes_every_example_and_the_workflow(self):
        readme = (EXAMPLES / "README.md").read_text(encoding="utf-8")

        for name in NAMES:
            assert f"packages/{name}" in readme
        for command in ("alx validate", "alx pack", "alx publish", "alx search", "alx pull"):
            assert command in readme
        for variable in ("ALX_URL", "ALX_TOKEN"):
            assert variable in readme
        assert "build_db.py" in readme

    @pytest.mark.parametrize("name", NAMES)
    def test_no_build_junk_lives_in_the_examples(self, name: str):
        offenders = [
            path
            for path in package_dir(name).rglob("*")
            if path.name in JUNK_NAMES or path.suffix == ".pyc"
        ]

        assert offenders == []


class TestPackages:
    @pytest.mark.parametrize("name", NAMES)
    def test_the_example_is_a_valid_package_with_the_expected_identity(self, name: str):
        expected = EXPECTED[name]

        parsed = pack_directory(package_dir(name)).parsed

        assert parsed.kind == expected.kind
        assert parsed.name == name
        assert parsed.version == expected.version
        assert parsed.entrypoint == expected.entrypoint
        assert parsed.root_prefix == ""
        assert parsed.display_name != ""

    @pytest.mark.parametrize("name", NAMES)
    def test_the_same_package_is_accepted_by_the_inspector_from_a_zip_file(
        self, name: str, tmp_path: Path
    ):
        output = tmp_path / "package.zip"
        assert run("pack", package_dir(name), "-o", output).code == 0

        parsed = inspect_package(output.read_bytes())

        assert (parsed.kind, parsed.name, parsed.version) == (
            EXPECTED[name].kind,
            name,
            EXPECTED[name].version,
        )

    @pytest.mark.parametrize("name", NAMES)
    def test_the_frontmatter_is_complete(self, name: str):
        data = frontmatter(name)

        assert REQUIRED_FRONTMATTER <= set(data)
        assert isinstance(data["version"], str)
        assert len(data["description"]) >= 80
        assert len(data["tags"]) >= 4
        assert len(set(data["tags"])) == len(data["tags"])
        assert re.fullmatch(r"[a-z0-9][a-z0-9-]*", data["category"])
        assert data["display_name"].strip()

    @pytest.mark.parametrize("name", NAMES)
    def test_the_parsed_metadata_reflects_the_frontmatter(self, name: str):
        data = frontmatter(name)

        parsed = pack_directory(package_dir(name)).parsed

        assert parsed.tags == data["tags"]
        assert parsed.category == data["category"]
        assert parsed.description == data["description"]
        assert parsed.display_name == data["display_name"]
        assert parsed.metadata == {}

    @pytest.mark.parametrize("name", NAMES)
    def test_the_expected_files_are_in_the_manifest(self, name: str):
        manifest = {entry.path for entry in pack_directory(package_dir(name)).parsed.manifest}

        assert set(EXPECTED[name].files) <= manifest

    @pytest.mark.parametrize("name", NAMES)
    def test_every_file_of_the_directory_is_packaged(self, name: str):
        on_disk = {
            path.relative_to(package_dir(name)).as_posix()
            for path in package_dir(name).rglob("*")
            if path.is_file()
        }

        manifest = {entry.path for entry in pack_directory(package_dir(name)).parsed.manifest}

        assert manifest == on_disk

    @pytest.mark.parametrize("name", NAMES)
    def test_the_packages_are_small(self, name: str):
        parsed = pack_directory(package_dir(name)).parsed

        assert parsed.size_bytes < 100 * 1024
        assert parsed.file_count <= 10

    @pytest.mark.parametrize("name", NAMES)
    def test_the_entrypoint_links_resolve_to_real_files(self, name: str):
        text = (package_dir(name) / EXPECTED[name].entrypoint).read_text(encoding="utf-8")
        links = re.findall(r"\]\(((?!https?:|#|mailto:)[^)\s]+)\)", text)

        for link in links:
            assert (package_dir(name) / link).is_file(), link

    @pytest.mark.parametrize("name", NAMES)
    def test_every_script_mentioned_by_the_entrypoint_exists(self, name: str):
        text = (package_dir(name) / EXPECTED[name].entrypoint).read_text(encoding="utf-8")
        mentioned = set(
            re.findall(r"`((?:scripts|templates|tables|references|spec|site|db)/[\w./-]+)", text)
        )

        for path in mentioned:
            assert (package_dir(name) / path).exists(), path


class TestDeterminism:
    @pytest.mark.parametrize("name", NAMES)
    def test_packing_twice_gives_the_same_bytes(self, name: str):
        assert pack_directory(package_dir(name)).data == pack_directory(package_dir(name)).data

    @pytest.mark.parametrize("name", NAMES)
    def test_a_copy_with_new_timestamps_in_another_place_gives_the_same_package(
        self, name: str, tmp_path: Path
    ):
        copy = tmp_path / "somewhere" / "else" / name
        shutil.copytree(package_dir(name), copy, copy_function=shutil.copy)
        for path in copy.rglob("*"):
            os.utime(path, (1_000_000_000, 1_100_000_000))

        assert pack_directory(copy).data == pack_directory(package_dir(name)).data

    @pytest.mark.parametrize("name", NAMES)
    def test_the_cli_and_the_library_agree_on_the_hash(self, name: str, tmp_path: Path):
        output = tmp_path / "o.zip"

        reported = run("pack", package_dir(name), "-o", output, "--json").json()

        assert reported["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
        assert reported["sha256"] == pack_directory(package_dir(name)).parsed.sha256

    @pytest.mark.parametrize("name", NAMES)
    def test_only_shell_scripts_are_executable(self, name: str, tmp_path: Path):
        output = tmp_path / "o.zip"
        run("pack", package_dir(name), "-o", output)

        with zipfile.ZipFile(output) as archive:
            modes = {info.filename: info.external_attr >> 16 for info in archive.infolist()}

        for path, mode in modes.items():
            executable = path.endswith(".sh")
            assert mode == (0o100755 if executable else 0o100644), path
            on_disk = package_dir(name) / path
            assert bool(on_disk.stat().st_mode & stat.S_IXUSR) is executable, path


class TestSourceRules:
    @pytest.mark.parametrize("script", python_scripts(), ids=lambda path: path.name)
    def test_the_scripts_have_no_comments(self, script: Path):
        source = script.read_text(encoding="utf-8")

        comments = [
            token.string
            for token in tokenize.generate_tokens(io.StringIO(source).readline)
            if token.type == tokenize.COMMENT
        ]

        assert comments == []

    @pytest.mark.parametrize("script", python_scripts(), ids=lambda path: path.name)
    def test_the_scripts_have_no_docstrings(self, script: Path):
        tree = ast.parse(script.read_text(encoding="utf-8"))

        documented = [
            getattr(node, "name", "<module>")
            for node in ast.walk(tree)
            if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and ast.get_docstring(node, clean=False) is not None
        ]

        assert documented == []

    @pytest.mark.parametrize("script", python_scripts(), ids=lambda path: path.name)
    def test_the_scripts_use_only_the_standard_library(self, script: Path):
        tree = ast.parse(script.read_text(encoding="utf-8"))
        imported = {
            name.split(".")[0]
            for node in ast.walk(tree)
            for name in (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
        }

        assert imported <= set(sys.stdlib_module_names)

    def test_the_shell_script_is_valid_and_strict(self):
        script = package_dir("changelog-from-commits") / "scripts" / "collect_commits.sh"

        checked = subprocess.run(
            ["sh", "-n", str(script)], capture_output=True, text=True, check=False
        )

        assert checked.returncode == 0
        lines = script.read_text().splitlines()
        assert lines[0] == "#!/bin/sh"
        assert "set -eu" in lines


class TestChangelogScripts:
    SCRIPT = package_dir("changelog-from-commits") / "scripts" / "format_changelog.py"
    COLLECT = package_dir("changelog-from-commits") / "scripts" / "collect_commits.sh"

    @staticmethod
    def records(*commits: tuple[str, str, str]) -> str:
        return "".join(f"{sha}\x1f{subject}\x1f{body}\x1e\n" for sha, subject, body in commits)

    def format(self, stream: str, *args: str):
        return run_script(self.SCRIPT, *args, stdin=stream)

    def test_the_documented_example_is_what_the_script_produces(self):
        stream = self.records(
            ("a1b2c3d4e5f6", "feat(api): add pagination to the search endpoint", ""),
            ("c7d8e9f0a1b2", "fix: stop crashing on empty queries", ""),
            ("e4f5a6b7c8d9", "refactor!: rename the page parameter to cursor", ""),
            ("0123456789ab", "chore: update lock file", ""),
        )

        result = self.format(stream, "--version", "2.0.0", "--date", "2026-10-16")

        assert result.returncode == 0
        assert result.stdout == (
            "## [2.0.0] - 2026-10-16\n\n"
            "### Added\n\n"
            "- **api:** add pagination to the search endpoint (a1b2c3d)\n\n"
            "### Changed\n\n"
            "- **BREAKING:** rename the page parameter to cursor (e4f5a6b)\n\n"
            "### Fixed\n\n"
            "- stop crashing on empty queries (c7d8e9f)\n\n"
        )
        assert "1 commit(s) left out" in result.stderr
        documented = (package_dir("changelog-from-commits") / "SKILL.md").read_text()
        for line in result.stdout.splitlines():
            if line.startswith("- "):
                assert line.split(" (")[0] in documented

    def test_sections_follow_the_changelog_order_and_breaking_entries_come_first(self):
        stream = self.records(
            ("1111111", "fix: a fix", ""),
            ("2222222", "security: close a hole", ""),
            ("3333333", "revert: bring back nothing", ""),
            ("4444444", "deprecate: old api", ""),
            ("5555555", "perf: faster", ""),
            ("6666666", "feat: new thing", ""),
            ("7777777", "feat!: big new thing", ""),
        )

        result = self.format(stream, "--version", "1.0.0", "--date", "2026-01-01")

        headings = [line for line in result.stdout.splitlines() if line.startswith("### ")]
        assert headings == [
            "### Added",
            "### Changed",
            "### Deprecated",
            "### Removed",
            "### Fixed",
            "### Security",
        ]
        added = result.stdout.split("### Added\n\n")[1].split("\n\n")[0]
        changed = result.stdout.split("### Changed\n\n")[1].split("\n\n")[0].splitlines()
        assert added == "- new thing (6666666)"
        assert changed == ["- **BREAKING:** big new thing (7777777)", "- faster (5555555)"]

    def test_a_breaking_footer_marks_any_commit_type_and_lists_its_note(self):
        stream = self.records(
            (
                "aaaaaaa",
                "docs: rewrite the guide",
                "Explains things.\n\nBREAKING CHANGE: the old guide is gone",
            ),
            ("bbbbbbb", "not conventional at all", "BREAKING-CHANGE: callers must update"),
        )

        result = self.format(stream, "--version", "1.0.0", "--date", "2026-01-01")

        assert (
            "- **BREAKING:** rewrite the guide (aaaaaaa)\n  - the old guide is gone\n"
            in result.stdout
        )
        assert (
            "- **BREAKING:** not conventional at all (bbbbbbb)\n  - callers must update\n"
            in result.stdout
        )
        assert "### Other" not in result.stdout

    def test_internal_and_free_form_commits_are_listed_only_on_request(self):
        stream = self.records(
            ("aaaaaaa", "docs: update readme", ""),
            ("bbbbbbb", "just words", ""),
            ("ccccccc", "feat: real change", ""),
        )

        quiet = self.format(stream, "--version", "1.0.0", "--date", "2026-01-01")
        verbose = self.format(
            stream, "--version", "1.0.0", "--date", "2026-01-01", "--include-other"
        )

        assert "Other" not in quiet.stdout
        assert "2 commit(s) left out" in quiet.stderr
        assert "- update readme (aaaaaaa)" in verbose.stdout
        assert "- just words (bbbbbbb)" in verbose.stdout
        assert verbose.stdout.index("### Added") < verbose.stdout.index("### Other")
        assert verbose.stderr == ""

    def test_an_empty_range_says_there_are_no_notable_changes(self):
        for stream in ("", "\n", self.records(("aaaaaaa", "chore: tidy", ""))):
            result = self.format(stream, "--version", "1.0.0", "--date", "2026-01-01")

            assert result.returncode == 0
            assert result.stdout == "## [1.0.0] - 2026-01-01\n\n_No notable changes._\n\n"

    @pytest.mark.parametrize(
        ("commits", "expected"),
        [
            ((("a", "feat!: x", ""),), "major"),
            ((("a", "fix: x", "BREAKING CHANGE: y"),), "major"),
            ((("a", "feat: x", ""), ("b", "fix: y", "")), "minor"),
            ((("a", "fix: x", ""),), "patch"),
            ((("a", "perf: x", ""),), "patch"),
            ((("a", "refactor: x", ""),), "patch"),
            ((("a", "docs: x", ""), ("b", "chore: y", "")), "none"),
            ((), "none"),
        ],
    )
    def test_the_version_bump_follows_the_commits(self, commits, expected: str):
        result = self.format(self.records(*commits), "--suggest-bump")

        assert result.returncode == 0
        assert result.stdout == expected + "\n"

    def test_the_conventional_subject_grammar(self):
        stream = self.records(
            ("1111111", "feat(scope with spaces): ok", ""),
            ("2222222", "FEAT: uppercase type", ""),
            ("3333333", "feat:no space", ""),
            ("4444444", "feat(): empty scope", ""),
            ("5555555", "feat(a)(b): double scope", ""),
            ("6666666", "feat: ", ""),
            ("7777777", "fix : space before colon", ""),
        )

        result = self.format(
            stream, "--version", "1.0.0", "--date", "2026-01-01", "--include-other"
        )

        added = result.stdout.split("### Added")[1].split("###")[0]
        assert "- **scope with spaces:** ok (1111111)" in added
        assert "- uppercase type (2222222)" in added
        other = result.stdout.split("### Other")[1]
        for subject in ("feat:no space", "feat(): empty scope", "feat(a)(b): double scope"):
            assert subject in other
        assert "fix : space before colon" in other

    def test_records_without_a_subject_are_ignored(self):
        stream = "lonely-hash\x1e\n" + self.records(("abcdefg", "feat: kept", ""))

        result = self.format(stream, "--version", "1.0.0", "--date", "2026-01-01")

        assert result.returncode == 0
        assert "- kept (abcdefg)" in result.stdout
        assert "lonely-hash" not in result.stdout

    def test_hashes_are_shortened_to_seven_characters(self):
        stream = self.records(("0123456789abcdef", "feat: x", ""))

        result = self.format(stream, "--version", "1.0.0", "--date", "2026-01-01")

        assert "(0123456)" in result.stdout

    def test_records_may_come_from_a_file(self, tmp_path: Path):
        source = tmp_path / "records.txt"
        source.write_text(self.records(("abcdefg", "feat: from a file", "")), encoding="utf-8")

        result = run_script(
            self.SCRIPT, "--version", "1.0.0", "--date", "2026-01-01", "--input", source
        )

        assert result.returncode == 0
        assert "from a file" in result.stdout

    def test_the_default_date_is_today_in_utc(self):
        result = self.format("", "--version", "1.0.0")

        assert re.match(r"## \[1\.0\.0\] - \d{4}-\d{2}-\d{2}\n", result.stdout)

    @pytest.mark.parametrize(
        "args",
        [
            [],
            ["--version", "1.0"],
            ["--version", "v1.0.0"],
            ["--version", "1.0.0", "--date", "16/10/2026"],
            ["--version", "1.0.0", "--date", "2026-1-1"],
            ["--version", "1.0.0\n"],
            ["--version", "1.0.0", "--date", "2026-01-01\n"],
            ["--bogus"],
        ],
    )
    def test_bad_arguments_exit_with_two(self, args: list[str]):
        result = self.format("", *args)

        assert result.returncode == 2
        assert result.stdout == ""
        assert "error" in result.stderr

    def test_a_missing_input_file_is_an_error_for_the_caller(self, tmp_path: Path):
        result = run_script(self.SCRIPT, "--version", "1.0.0", "--input", tmp_path / "nope")

        assert result.returncode == 1
        assert result.stderr.startswith("error: ")
        assert "Traceback" not in result.stderr

    def test_prereleases_are_accepted_as_versions(self):
        result = self.format("", "--version", "2.0.0-rc.1", "--date", "2026-01-01")

        assert result.stdout.startswith("## [2.0.0-rc.1] - 2026-01-01")

    @pytest.fixture
    def fake_git(self, tmp_path: Path) -> dict[str, str]:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "git.log"
        git = bin_dir / "git"
        git.write_text(
            "#!/bin/sh\n"
            'echo "$@" >> "$GIT_LOG"\n'
            'case "$1" in\n'
            '  describe) if [ -n "${FAKE_TAG:-}" ]; then echo "$FAKE_TAG"; exit 0; fi; exit 128 ;;\n'
            "  log) printf 'abc1234\\037feat: from fake git\\037\\036\\n' ;;\n"
            "esac\n"
        )
        git.chmod(0o755)
        return {"PATH": f"{bin_dir}:/usr/bin:/bin", "GIT_LOG": str(log)}

    def collect(self, env: dict[str, str], *args: str):
        return subprocess.run(
            ["sh", str(self.COLLECT), *args], capture_output=True, text=True, env=env, check=False
        )

    def test_without_a_tag_the_whole_history_is_collected(self, fake_git: dict[str, str]):
        result = self.collect(fake_git)

        assert result.returncode == 0
        calls = Path(fake_git["GIT_LOG"]).read_text().splitlines()
        assert calls[0] == "describe --tags --abbrev=0"
        assert calls[1].endswith("HEAD")
        assert calls[1].startswith("log --no-merges --format=")
        assert "\x1f" in result.stdout

    def test_with_a_tag_the_range_starts_at_it(self, fake_git: dict[str, str]):
        result = self.collect({**fake_git, "FAKE_TAG": "v1.2.0"})

        assert result.returncode == 0
        calls = Path(fake_git["GIT_LOG"]).read_text().splitlines()
        assert calls[1].endswith("v1.2.0..HEAD")

    def test_an_explicit_range_is_used_as_given(self, fake_git: dict[str, str]):
        result = self.collect({**fake_git, "FAKE_TAG": "v9.9.9"}, "v1.0.0..v1.1.0")

        assert result.returncode == 0
        calls = Path(fake_git["GIT_LOG"]).read_text().splitlines()
        assert calls == [calls[0]] and calls[0].endswith("v1.0.0..v1.1.0")

    def test_the_collected_stream_feeds_the_formatter(self, fake_git: dict[str, str]):
        collected = self.collect(fake_git)

        result = self.format(collected.stdout, "--version", "1.0.0", "--date", "2026-01-01")

        assert "- from fake git (abc1234)" in result.stdout

    def test_a_failing_git_stops_the_script(self, tmp_path: Path):
        (tmp_path / "bin").mkdir()
        failing = tmp_path / "bin" / "git"
        failing.write_text("#!/bin/sh\nexit 3\n")
        failing.chmod(0o755)

        result = self.collect({"PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin"})

        assert result.returncode == 3


class TestPostmortemRenderer:
    SCRIPT = package_dir("incident-postmortem") / "scripts" / "render_postmortem.py"
    EXAMPLE = package_dir("incident-postmortem") / "references" / "example-incident.json"
    TEMPLATE = package_dir("incident-postmortem") / "templates" / "postmortem.html"

    def example(self) -> dict:
        return json.loads(self.EXAMPLE.read_text(encoding="utf-8"))

    def write(self, tmp_path: Path, data: object, name: str = "incident.json") -> Path:
        path = tmp_path / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_the_example_renders_to_a_complete_page(self, tmp_path: Path):
        output = tmp_path / "report.html"

        result = run_script(self.SCRIPT, self.EXAMPLE, "-o", output)

        assert result.returncode == 0
        assert result.stdout == "" and result.stderr == ""
        page = output.read_text(encoding="utf-8")
        assert "{{" not in page and "}}" not in page
        assert (
            "<title>Postmortem INC-2026-041: Search results were empty for most users</title>"
            in page
        )
        assert "<dd>1h 12m</dd>" in page
        assert page.count('<tr><td class="time">') == 7
        assert page.count("<li>") == 5
        assert "<td>Search team</td><td>Add a deploy check" in page
        assert "<dd>SEV2</dd>" in page and "<dd>resolved</dd>" in page

    def test_the_output_defaults_to_standard_output(self):
        result = run_script(self.SCRIPT, self.EXAMPLE)

        assert result.returncode == 0
        assert result.stdout.startswith("<!doctype html>")

    def test_the_rendering_is_deterministic(self):
        first = run_script(self.SCRIPT, self.EXAMPLE).stdout
        second = run_script(self.SCRIPT, self.EXAMPLE).stdout

        assert first == second

    def test_every_value_is_escaped(self, tmp_path: Path):
        hostile = '<script>alert("x")</script> & <img src=x onerror=y>'
        data = self.example()
        for field in ("title", "incident_id", "summary", "impact", "root_cause"):
            data[field] = hostile
        data["timeline"] = [{"time": hostile, "event": hostile}]
        data["what_went_well"] = [hostile]
        data["what_went_wrong"] = [hostile]
        data["action_items"] = [{"owner": hostile, "action": hostile, "due": hostile}]

        result = run_script(self.SCRIPT, self.write(tmp_path, data))

        assert result.returncode == 0
        assert "<script" not in result.stdout
        assert "<img" not in result.stdout
        assert html.escape(hostile) in result.stdout

    def test_data_cannot_inject_placeholders(self, tmp_path: Path):
        data = self.example()
        data["summary"] = "{{impact}} and {{title}}"
        data["title"] = "{{root_cause}}"

        result = run_script(self.SCRIPT, self.write(tmp_path, data))

        assert result.returncode == 0
        assert "<p>{{impact}} and {{title}}</p>" in result.stdout
        assert "<h1>{{root_cause}}</h1>" in result.stdout

    def test_paragraphs_follow_blank_lines(self, tmp_path: Path):
        data = self.example()
        data["summary"] = "one\ntwo\n\n\nthree\n  \nfour"

        result = run_script(self.SCRIPT, self.write(tmp_path, data))

        assert "<p>one two</p>\n<p>three</p>\n<p>four</p>" in result.stdout

    def test_empty_lists_of_notes_say_so(self, tmp_path: Path):
        data = self.example()
        data["what_went_well"] = []
        del data["what_went_wrong"]

        result = run_script(self.SCRIPT, self.write(tmp_path, data))

        assert result.returncode == 0
        assert result.stdout.count("<li>Nothing recorded.</li>") == 2

    def test_an_incident_in_progress_has_no_resolution(self, tmp_path: Path):
        data = self.example()
        del data["resolved_at"]
        data["status"] = "investigating"

        result = run_script(self.SCRIPT, self.write(tmp_path, data))

        assert result.returncode == 0
        assert "<dt>Resolved</dt><dd>ongoing</dd>" in result.stdout
        assert "<dt>Duration</dt><dd>n/a</dd>" in result.stdout

    @pytest.mark.parametrize(
        ("started", "resolved", "duration"),
        [
            ("2026-10-02T09:00:00Z", "2026-10-02T09:45:00Z", "45m"),
            ("2026-10-02T09:00:00Z", "2026-10-02T09:00:00Z", "0m"),
            ("2026-10-02T09:00:00+00:00", "2026-10-03T10:30:59+00:00", "25h 30m"),
            ("2026-10-02T09:00:00", "2026-10-02T11:00:00", "2h 0m"),
            ("2026-10-02T09:00:00+02:00", "2026-10-02T09:00:00Z", "2h 0m"),
        ],
    )
    def test_the_duration_is_computed_from_the_timestamps(
        self, tmp_path: Path, started: str, resolved: str, duration: str
    ):
        data = self.example()
        data["started_at"], data["resolved_at"] = started, resolved

        result = run_script(self.SCRIPT, self.write(tmp_path, data))

        assert f"<dt>Duration</dt><dd>{duration}</dd>" in result.stdout

    def test_all_the_problems_are_reported_at_once_and_nothing_is_written(self, tmp_path: Path):
        output = tmp_path / "report.html"
        data = self.example()
        data["severity"] = "SEV9"
        data["status"] = "dunno"
        data["summary"] = "   "
        data["timeline"] = []
        data["action_items"] = [{"owner": "x"}]
        data["what_went_well"] = "not a list"
        del data["title"]

        result = run_script(self.SCRIPT, self.write(tmp_path, data), "-o", output)

        assert result.returncode == 1
        assert result.stdout == ""
        errors = result.stderr.splitlines()
        assert all(line.startswith("error: ") for line in errors)
        joined = "\n".join(errors)
        for fragment in (
            "title:",
            "summary:",
            "severity: must be one of SEV1, SEV2, SEV3, SEV4",
            "status: must be one of investigating, mitigated, resolved",
            "timeline: a non-empty list is required",
            "action_items[1]: needs the text fields owner, action, due",
            "what_went_well: must be a list of non-empty strings",
        ):
            assert fragment in joined
        assert not output.exists()

    @pytest.mark.parametrize(
        ("changes", "fragment"),
        [
            ({"started_at": "yesterday"}, "started_at: must be an ISO 8601 timestamp"),
            ({"resolved_at": "later"}, "resolved_at: must be an ISO 8601 timestamp"),
            ({"resolved_at": 5}, "resolved_at: must be an ISO 8601 timestamp"),
            ({"resolved_at": "2026-10-02T08:00:00Z"}, "resolved_at: must not be earlier"),
            ({"resolved_at": "2026-10-02T10:26:00"}, "same timezone convention"),
            ({"timeline": [{"time": 930, "event": "x"}]}, "timeline[1]"),
            ({"timeline": "nope"}, "timeline: a non-empty list is required"),
            ({"what_went_wrong": [1, 2]}, "what_went_wrong: must be a list"),
            ({"severity": ["SEV1"]}, "severity: must be one of"),
        ],
    )
    def test_individual_problems_are_diagnosed(self, tmp_path: Path, changes: dict, fragment: str):
        data = self.example()
        data.update(changes)

        result = run_script(self.SCRIPT, self.write(tmp_path, data))

        assert result.returncode == 1
        assert fragment in result.stderr

    @pytest.mark.parametrize("content", ["[]", '"text"', "42", "null"])
    def test_the_input_must_be_a_json_object(self, tmp_path: Path, content: str):
        path = tmp_path / "bad.json"
        path.write_text(content)

        result = run_script(self.SCRIPT, path)

        assert result.returncode == 1
        assert "error: the input must be a JSON object" in result.stderr

    def test_unreadable_input_is_reported_without_a_traceback(self, tmp_path: Path):
        broken = tmp_path / "broken.json"
        broken.write_text("{not json")

        for target in (broken, tmp_path / "missing.json"):
            result = run_script(self.SCRIPT, target)

            assert result.returncode == 1
            assert result.stderr.startswith("error: ")
            assert "Traceback" not in result.stderr

    def test_an_unwritable_output_is_reported(self, tmp_path: Path):
        result = run_script(self.SCRIPT, self.EXAMPLE, "-o", tmp_path / "no" / "dir" / "x.html")

        assert result.returncode == 1
        assert result.stderr.startswith("error: ")

    def test_a_template_with_unknown_placeholders_is_rejected(self, tmp_path: Path):
        template = tmp_path / "t.html"
        template.write_text("<p>{{title}} {{nonsense}} {{other_one}}</p>")

        result = run_script(self.SCRIPT, self.EXAMPLE, "--template", template)

        assert result.returncode == 1
        assert "unknown placeholders: nonsense, other_one" in result.stderr
        assert result.stdout == ""

    def test_a_custom_template_may_use_a_subset_of_the_placeholders(self, tmp_path: Path):
        template = tmp_path / "t.html"
        template.write_text("[{{severity}}|{{duration}}|{{resolved_at}}]")

        result = run_script(self.SCRIPT, self.EXAMPLE, "--template", template)

        assert result.stdout == "[SEV2|1h 12m|2026-10-02T10:26:00Z]"

    def test_the_example_input_is_valid_for_every_documented_field(self):
        data = self.example()
        documented = (
            package_dir("incident-postmortem") / "references" / "template-fields.md"
        ).read_text()

        for field in data:
            assert f"`{field}`" in documented

    def test_the_template_uses_exactly_the_documented_placeholders(self):
        used = set(re.findall(r"\{\{([a-z_]+)\}\}", self.TEMPLATE.read_text()))
        documented = (
            package_dir("incident-postmortem") / "references" / "template-fields.md"
        ).read_text()

        assert used == {
            "title",
            "incident_id",
            "severity",
            "status",
            "started_at",
            "resolved_at",
            "duration",
            "summary",
            "impact",
            "root_cause",
            "timeline_rows",
            "went_well_items",
            "went_wrong_items",
            "action_rows",
        }
        for placeholder in used:
            assert "{{" + placeholder + "}}" in documented


VOID_TAGS = frozenset({"meta", "link", "br", "hr", "img", "input"})
HTML_FILES = (
    package_dir("http-reference") / "site" / "cheatsheet.html",
    package_dir("incident-postmortem") / "templates" / "postmortem.html",
)
HEURISTICALLY_CACHEABLE = frozenset({200, 203, 204, 206, 300, 301, 308, 404, 405, 410, 414, 501})


class Outline(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.tags: list[str] = []
        self.problems: list[str] = []
        self.references: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        self.tags.append(tag)
        for key, value in attrs:
            if key in {"src", "href", "action"} and value:
                self.references.append(value)
        if tag not in VOID_TAGS:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack[-1] != tag:
            self.problems.append(f"unexpected </{tag}> with {self.stack}")
        else:
            self.stack.pop()


class TestHtmlFiles:
    @pytest.mark.parametrize("path", HTML_FILES, ids=lambda path: path.name)
    def test_the_page_is_well_formed_and_self_contained(self, path: Path):
        text = path.read_text(encoding="utf-8")
        outline = Outline()
        outline.feed(text)
        outline.close()

        assert text.startswith("<!doctype html>")
        assert outline.problems == [] and outline.stack == []
        assert "script" not in outline.tags
        assert not [ref for ref in outline.references if re.match(r"(https?:)?//", ref)]
        assert {"html", "head", "title", "body", "style", "meta"} <= set(outline.tags)
        assert '<html lang="en">' in text
        assert '<meta charset="utf-8">' in text
        assert 'name="viewport"' in text

    @pytest.mark.parametrize("path", HTML_FILES, ids=lambda path: path.name)
    def test_the_page_has_no_inline_event_handlers(self, path: Path):
        assert not re.search(r"\son\w+\s*=", path.read_text(encoding="utf-8"), re.IGNORECASE)

    def test_the_rendered_report_is_well_formed(self, tmp_path: Path):
        script = package_dir("incident-postmortem") / "scripts" / "render_postmortem.py"
        example = package_dir("incident-postmortem") / "references" / "example-incident.json"

        page = run_script(script, example).stdout
        outline = Outline()
        outline.feed(page)

        assert outline.problems == [] and outline.stack == []
        assert outline.tags.count("tr") == 1 + 7 + 1 + 4


def normalised_snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        path: (content, 0o755 if mode & stat.S_IXUSR else 0o644)
        for path, (content, mode) in tree_snapshot(root).items()
    }


def read_csv(name: str, table: str) -> list[dict[str, str]]:
    path = package_dir(name) / "tables" / f"{table}.csv"
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


class TestHttpReferenceData:
    def status_codes(self) -> list[dict[str, str]]:
        return read_csv("http-reference", "status_codes")

    def test_status_codes_are_unique_sorted_and_classified_by_their_first_digit(self):
        rows = self.status_codes()
        codes = [int(row["code"]) for row in rows]
        classes = {
            "1": "informational",
            "2": "success",
            "3": "redirection",
            "4": "client_error",
            "5": "server_error",
        }

        assert codes == sorted(set(codes))
        assert all(100 <= code <= 599 for code in codes)
        assert all(row["class"] == classes[row["code"][0]] for row in rows)
        assert all(row["name"] and row["summary"] and row["rfc"].startswith("RFC ") for row in rows)

    def test_default_cacheability_matches_the_list_of_rfc_9110(self):
        cacheable = {
            int(row["code"]) for row in self.status_codes() if row["cacheable_by_default"] == "yes"
        }

        assert cacheable == HEURISTICALLY_CACHEABLE
        assert {row["cacheable_by_default"] for row in self.status_codes()} == {"yes", "no"}

    def test_the_codes_used_by_this_registry_are_present(self):
        codes = {int(row["code"]) for row in self.status_codes()}

        assert {200, 201, 204, 400, 401, 403, 404, 409, 413, 422, 429, 500, 503} <= codes

    def test_methods_follow_the_semantics_of_rfc_9110(self):
        methods = {row["method"]: row for row in read_csv("http-reference", "methods")}

        assert set(methods) == {
            "GET",
            "HEAD",
            "POST",
            "PUT",
            "DELETE",
            "CONNECT",
            "OPTIONS",
            "TRACE",
            "PATCH",
        }
        safe = {name for name, row in methods.items() if row["safe"] == "yes"}
        idempotent = {name for name, row in methods.items() if row["idempotent"] == "yes"}
        assert safe == {"GET", "HEAD", "OPTIONS", "TRACE"}
        assert idempotent == safe | {"PUT", "DELETE"}
        assert methods["PATCH"]["rfc"] == "RFC 5789"

    def test_every_scenario_points_to_a_known_status_code(self):
        codes = {int(row["code"]) for row in self.status_codes()}
        scenarios = read_csv("http-reference", "scenarios")

        assert all(int(row["status"]) in codes for row in scenarios)
        assert len({row["scenario"] for row in scenarios}) == len(scenarios)
        assert all(row["scenario"] and row["notes"] for row in scenarios)

    def test_the_index_states_the_real_row_counts(self):
        index = (package_dir("http-reference") / "INDEX.md").read_text(encoding="utf-8")

        for table, label in (
            ("status_codes", "registered status codes"),
            ("methods", "request methods"),
            ("scenarios", "everyday situations"),
        ):
            assert f"{len(read_csv('http-reference', table))} {label}" in index

    def test_the_index_documents_every_column_of_every_table(self):
        index = (package_dir("http-reference") / "INDEX.md").read_text(encoding="utf-8")

        for table in ("status_codes", "methods", "scenarios"):
            assert f"`tables/{table}.csv`" in index
            for column in read_csv("http-reference", table)[0]:
                assert f"`{column}`" in index

    def test_the_problem_details_schema_is_valid_yaml_with_the_standard_members(self):
        path = package_dir("http-reference") / "spec" / "problem-details.yaml"

        spec = yaml.safe_load(path.read_text(encoding="utf-8"))

        assert set(spec["properties"]) == {"type", "title", "status", "detail", "instance"}
        assert spec["properties"]["status"]["minimum"] == 100
        assert spec["properties"]["status"]["maximum"] == 599
        assert spec["additionalProperties"] is True
        for sample in spec["examples"]:
            assert 100 <= sample["status"] <= 599
            assert set(sample) >= {"type", "title", "status"}

    def test_the_yaml_spec_has_no_anchors_or_aliases(self):
        text = (package_dir("http-reference") / "spec" / "problem-details.yaml").read_text()

        events = list(yaml.parse(text))

        assert not [event for event in events if getattr(event, "anchor", None)]

    def test_the_media_types_of_the_files_are_the_expected_ones(self):
        manifest = {
            entry.path: entry
            for entry in pack_directory(package_dir("http-reference")).parsed.manifest
        }

        assert manifest["tables/status_codes.csv"].media_type == "text/csv"
        assert manifest["spec/problem-details.yaml"].media_type == "application/yaml"
        assert manifest["site/cheatsheet.html"].media_type == "text/html"
        assert manifest["INDEX.md"].media_type == "text/markdown"
        assert manifest["scripts/build_db.py"].media_type == "text/x-python"
        sqlite = manifest["db/http-reference.sqlite"]
        assert sqlite.media_type == "application/vnd.sqlite3"
        assert sqlite.text is False
        assert all(entry.text for path, entry in manifest.items() if path != sqlite.path)


class TestHttpReferenceDatabase:
    SCRIPT = package_dir("http-reference") / "scripts" / "build_db.py"
    DATABASE = package_dir("http-reference") / "db" / "http-reference.sqlite"

    @staticmethod
    def dump(path: Path) -> list[str]:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return list(connection.iterdump())
        finally:
            connection.close()

    def build(self, tmp_path: Path, name: str = "db.sqlite", *extra: object):
        output = tmp_path / name
        return output, run_script(self.SCRIPT, "--output", output, *extra)

    def test_the_shipped_database_is_healthy(self):
        connection = sqlite3.connect(f"file:{self.DATABASE}?mode=ro", uri=True)
        try:
            assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
            assert connection.execute("PRAGMA user_version").fetchone() == (1,)
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            views = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='view'")
            }
        finally:
            connection.close()

        assert tables == {"status_codes", "methods", "scenarios"}
        assert views == {"scenario_details"}

    def test_the_database_holds_exactly_the_rows_of_the_csv_files(self):
        connection = sqlite3.connect(f"file:{self.DATABASE}?mode=ro", uri=True)
        try:
            stored = connection.execute(
                "SELECT code, name, class, summary, cacheable_by_default, rfc "
                "FROM status_codes ORDER BY code"
            ).fetchall()
            methods = connection.execute("SELECT name, safe, idempotent FROM methods").fetchall()
            scenarios = connection.execute(
                "SELECT id, scenario, status, headers, notes FROM scenarios ORDER BY id"
            ).fetchall()
        finally:
            connection.close()

        status_rows = read_csv("http-reference", "status_codes")
        assert stored == [
            (
                int(row["code"]),
                row["name"],
                row["class"],
                row["summary"],
                1 if row["cacheable_by_default"] == "yes" else 0,
                row["rfc"],
            )
            for row in status_rows
        ]
        assert sorted(methods) == sorted(
            (row["method"], int(row["safe"] == "yes"), int(row["idempotent"] == "yes"))
            for row in read_csv("http-reference", "methods")
        )
        assert scenarios == [
            (number, row["scenario"], int(row["status"]), row["headers"], row["notes"])
            for number, row in enumerate(read_csv("http-reference", "scenarios"), start=1)
        ]

    def test_the_documented_queries_work(self):
        connection = sqlite3.connect(f"file:{self.DATABASE}?mode=ro", uri=True)
        try:
            cacheable = connection.execute(
                "SELECT code FROM status_codes WHERE class = 'client_error' "
                "AND cacheable_by_default = 1 ORDER BY code"
            ).fetchall()
            unsafe_idempotent = connection.execute(
                "SELECT name FROM methods WHERE idempotent = 1 AND safe = 0 ORDER BY name"
            ).fetchall()
            joined = connection.execute(
                "SELECT status, status_name FROM scenario_details WHERE scenario LIKE 'A duplicate create%'"
            ).fetchall()
        finally:
            connection.close()

        assert cacheable == [(404,), (405,), (410,), (414,)]
        assert unsafe_idempotent == [("DELETE",), ("PUT",)]
        assert joined == [(409, "Conflict")]

    def test_regenerating_the_database_gives_the_same_content(self, tmp_path: Path):
        output, result = self.build(tmp_path)

        assert result.returncode == 0
        assert result.stdout.startswith(f"wrote {output} with 53 status_codes, 9 methods, 21")
        assert self.dump(output) == self.dump(self.DATABASE)

    def test_regenerating_twice_gives_identical_bytes(self, tmp_path: Path):
        first, _ = self.build(tmp_path, "one.sqlite")
        second, _ = self.build(tmp_path, "two.sqlite")

        assert first.read_bytes() == second.read_bytes()

    def test_no_partial_files_are_left_behind(self, tmp_path: Path):
        self.build(tmp_path)

        assert sorted(item.name for item in tmp_path.iterdir()) == ["db.sqlite"]

    def test_a_changed_table_changes_the_database(self, tmp_path: Path):
        tables = tmp_path / "tables"
        shutil.copytree(package_dir("http-reference") / "tables", tables)
        path = tables / "status_codes.csv"
        path.write_text(path.read_text() + "299,Custom,success,custom,no,RFC 1\n")

        output, result = self.build(tmp_path, "custom.sqlite", "--tables", tables)

        assert result.returncode == 0
        assert "54 status_codes" in result.stdout
        connection = sqlite3.connect(f"file:{output}?mode=ro", uri=True)
        try:
            assert connection.execute(
                "SELECT name FROM status_codes WHERE code = 299"
            ).fetchone() == ("Custom",)
        finally:
            connection.close()

    @pytest.mark.parametrize(
        ("table", "edit", "fragment"),
        [
            (
                "status_codes",
                lambda text: text.replace("code,name", "id,name", 1),
                "expected the columns",
            ),
            ("status_codes", lambda text: text.replace(",yes,", ",maybe,", 1), "must be yes or no"),
            ("status_codes", lambda text: text + "600,Out,success,out,no,RFC 1\n", "CHECK"),
            ("status_codes", lambda text: text + "700,Out\n", "wrong number of fields"),
            (
                "status_codes",
                lambda text: text.replace("200,OK,success", "200,OK,weird", 1),
                "CHECK",
            ),
            ("scenarios", lambda text: text.replace(",201,", ",299,", 1), "foreign key"),
            (
                "methods",
                lambda text: text.replace("GET,yes", "GET,perhaps", 1),
                "must be yes or no",
            ),
            ("methods", lambda text: text + "GET,yes,yes,200,dup,RFC 1\n", "UNIQUE"),
        ],
    )
    def test_invalid_tables_are_rejected_without_touching_the_output(
        self, tmp_path: Path, table: str, edit, fragment: str
    ):
        tables = tmp_path / "tables"
        shutil.copytree(package_dir("http-reference") / "tables", tables)
        path = tables / f"{table}.csv"
        path.write_text(edit(path.read_text()))
        output = tmp_path / "keep.sqlite"
        output.write_bytes(b"previous content")

        result = run_script(self.SCRIPT, "--tables", tables, "--output", output)

        assert result.returncode == 1
        assert result.stdout == ""
        assert result.stderr.startswith("error: ")
        assert fragment.lower() in result.stderr.lower()
        assert "Traceback" not in result.stderr
        assert output.read_bytes() == b"previous content"
        assert sorted(item.name for item in tmp_path.iterdir()) == ["keep.sqlite", "tables"]

    def test_a_missing_table_is_reported(self, tmp_path: Path):
        result = run_script(self.SCRIPT, "--tables", tmp_path, "--output", tmp_path / "x.sqlite")

        assert result.returncode == 1
        assert result.stderr.startswith("error: ")

    def test_the_default_paths_are_inside_the_package(self):
        source = self.SCRIPT.read_text()

        assert 'PACKAGE_ROOT / "tables"' in source
        assert 'PACKAGE_ROOT / "db" / "http-reference.sqlite"' in source


class TestEndToEnd:
    def test_all_the_examples_survive_a_publish_search_and_pull_cycle(self, tmp_path: Path):
        registry = FakeRegistry()
        env = remote_env()

        for name in NAMES:
            published = run(
                "publish",
                package_dir(name),
                "--publish",
                transport=registry.transport,
                env=env,
            )
            assert published.code == 0, published.err
            assert published.out.startswith(f"Uploaded {name} {EXPECTED[name].version}")

        listing = run("search", "--json", transport=registry.transport, env=env).json()
        assert [item["name"] for item in listing["items"]] == NAMES
        assert {item["kind"] for item in listing["items"]} == {"skill", "grounding"}

        for name in NAMES:
            pulled = run("pull", name, "-d", tmp_path / name, transport=registry.transport, env=env)
            assert pulled.code == 0, pulled.err
            assert tree_snapshot(tmp_path / name) == normalised_snapshot(package_dir(name))

    def test_pulled_examples_still_work(self, tmp_path: Path):
        registry = FakeRegistry()
        for name in NAMES:
            run(
                "publish",
                package_dir(name),
                "--publish",
                transport=registry.transport,
                env=remote_env(),
            )
        run("pull", "http-reference", "-d", tmp_path / "ref", transport=registry.transport, env={})
        run(
            "pull",
            "incident-postmortem",
            "-d",
            tmp_path / "pm",
            transport=registry.transport,
            env={},
        )

        database = sqlite3.connect(
            f"file:{tmp_path / 'ref' / 'db' / 'http-reference.sqlite'}?mode=ro", uri=True
        )
        try:
            assert database.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert database.execute(
                "SELECT name FROM status_codes WHERE code = 404"
            ).fetchone() == ("Not Found",)
        finally:
            database.close()
        rendered = run_script(
            tmp_path / "pm" / "scripts" / "render_postmortem.py",
            tmp_path / "pm" / "references" / "example-incident.json",
        )
        assert rendered.returncode == 0
        assert "<h1>Search results were empty for most users</h1>" in rendered.stdout

    def test_the_search_finds_the_examples_by_what_they_are_about(self):
        registry = FakeRegistry()
        for name in NAMES:
            run(
                "publish",
                package_dir(name),
                "--publish",
                transport=registry.transport,
                env=remote_env(),
            )

        by_topic = {
            "changelog": ["changelog-from-commits"],
            "postmortem": ["incident-postmortem"],
            "status-codes": ["http-reference"],
        }

        for query, expected in by_topic.items():
            found = run("search", query, "--json", transport=registry.transport, env={}).json()
            assert [item["name"] for item in found["items"]] == expected
