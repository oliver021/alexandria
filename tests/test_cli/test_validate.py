from __future__ import annotations

import os
import re
import zipfile
from pathlib import Path

import pytest

from app.cli import pack_directory
from tests.helpers.packages import (
    corrupt_entry_data,
    index_md,
    make_bomb_zip,
    make_encrypted_zip,
    make_grounding_zip,
    make_skill_zip,
    make_symlink_zip,
    make_traversal_zip,
    make_zip,
    set_compression_method,
    skill_md,
)
from tests.test_cli.support import parse_fields, run, skill_tree, write_tree

pytestmark = pytest.mark.skipif(os.name == "nt", reason="relies on POSIX file semantics")

SHA256 = re.compile(r"sha256 ([0-9a-f]{64})")


class TestValidDirectories:
    def test_a_valid_skill_directory_exits_zero_and_prints_its_summary(self, tmp_path: Path):
        source = skill_tree(
            tmp_path / "pkg",
            "demo-skill",
            "1.2.3",
            description="Does a demo.",
            tags=["Alpha", "beta"],
            category="Demos",
        )

        result = run("validate", source)

        assert result.code == 0
        assert result.err == ""
        lines = result.out.splitlines()
        assert lines[0] == f"OK: {source}"
        fields = parse_fields(lines[1:])
        assert fields["kind"] == "skill"
        assert fields["name"] == "demo-skill"
        assert fields["version"] == "1.2.3"
        assert fields["description"] == "Does a demo."
        assert fields["category"] == "demos"
        assert fields["tags"] == "alpha, beta"
        assert fields["entrypoint"] == "SKILL.md"
        assert fields["display name"] == "Demo Skill"
        assert fields["files"].startswith("3 (")
        assert "uncompressed" in fields["files"]
        assert SHA256.search(fields["package"])

    def test_the_reported_sizes_match_the_packed_package(self, tmp_path: Path):
        source = skill_tree(tmp_path / "pkg", files={"data/blob.bin": bytes(range(256)) * 10})
        packed = pack_directory(source).parsed

        result = run("validate", source, "--json").json()

        assert result["file_count"] == packed.file_count == 4
        assert result["uncompressed_bytes"] == packed.uncompressed_bytes
        assert result["size_bytes"] == packed.size_bytes
        assert result["sha256"] == packed.sha256

    def test_a_grounding_directory_is_reported_as_grounding(self, tmp_path: Path):
        source = write_tree(
            tmp_path / "ground",
            {"INDEX.md": index_md("city-facts", "0.3.0"), "data/cities.csv": "a,b\n1,2\n"},
        )

        result = run("validate", source, "--json").json()

        assert result["valid"] is True
        assert (result["kind"], result["name"], result["version"]) == (
            "grounding",
            "city-facts",
            "0.3.0",
        )
        assert result["entrypoint"] == "INDEX.md"

    def test_files_flag_lists_the_manifest_in_path_order(self, tmp_path: Path):
        source = skill_tree(tmp_path / "pkg")

        result = run("validate", source, "--files")

        assert result.code == 0
        listed = result.out.split("manifest:\n", 1)[1].splitlines()
        assert [line.strip().split("  ")[0] for line in listed] == [
            "SKILL.md",
            "references/notes.md",
            "scripts/run.sh",
        ]

    def test_json_output_contains_the_summary_and_the_manifest(self, tmp_path: Path):
        source = skill_tree(tmp_path / "pkg", "demo-skill", "2.0.0-rc.1", tags=["x"])

        result = run("validate", source, "--json")

        assert result.code == 0
        assert result.err == ""
        report = result.json()
        assert report["valid"] is True
        assert report["path"] == str(source)
        assert report["version"] == "2.0.0-rc.1"
        assert report["tags"] == ["x"]
        assert report["category"] is None
        assert [item["path"] for item in report["files"]] == [
            "SKILL.md",
            "references/notes.md",
            "scripts/run.sh",
        ]
        assert set(report["files"][0]) == {"path", "size", "sha256", "media_type", "text"}

    def test_junk_directories_and_files_never_reach_the_package(self, tmp_path: Path):
        source = skill_tree(tmp_path / "pkg")
        write_tree(
            source,
            {
                ".git/HEAD": "ref\n",
                ".DS_Store": "x",
                "Thumbs.db": "x",
                "__pycache__/a.cpython-314.pyc": "x",
                "scripts/__pycache__/b.pyc": "x",
                "scripts/helper.pyc": "x",
                "node_modules/left-pad/index.js": "x",
                "references/node_modules/dep.js": "x",
                "references/.DS_Store": "x",
                "scripts/.git": "gitdir: elsewhere\n",
            },
        )

        report = run("validate", source, "--json").json()

        assert [item["path"] for item in report["files"]] == [
            "SKILL.md",
            "references/notes.md",
            "scripts/run.sh",
        ]

    def test_the_junk_filter_does_not_hide_lookalike_names(self, tmp_path: Path):
        source = skill_tree(
            tmp_path / "pkg",
            files={
                "pyc.txt": "x",
                "scripts/not_a_pyc": "x",
                "gitignore.md": "x",
                "git/readme": "x",
            },
        )

        paths = [item["path"] for item in run("validate", source, "--json").json()["files"]]

        assert {"pyc.txt", "scripts/not_a_pyc", "gitignore.md", "git/readme"} <= set(paths)

    def test_a_directory_with_a_single_wrapper_folder_is_accepted(self, tmp_path: Path):
        source = write_tree(tmp_path / "outer", {"inner/SKILL.md": skill_md("wrapped-skill")})

        result = run("validate", source, "--json")

        assert result.code == 0
        assert result.json()["name"] == "wrapped-skill"
        assert [item["path"] for item in result.json()["files"]] == ["SKILL.md"]

    def test_a_symlinked_package_root_is_followed(self, tmp_path: Path):
        source = skill_tree(tmp_path / "real")
        os.symlink(source, tmp_path / "link")

        assert run("validate", tmp_path / "link").code == 0

    def test_version_option_supplies_a_missing_frontmatter_version(self, tmp_path: Path):
        source = write_tree(tmp_path / "pkg", {"SKILL.md": skill_md("demo-skill", None)})

        without = run("validate", source, "--json")
        with_option = run("validate", source, "--version", "3.1.4", "--json")

        assert without.code == 1
        assert without.json()["errors"][0]["reason"] == "MISSING_FIELD"
        assert with_option.code == 0
        assert with_option.json()["version"] == "3.1.4"


class TestValidZips:
    def test_a_valid_zip_exits_zero(self, tmp_path: Path):
        archive = tmp_path / "demo.zip"
        archive.write_bytes(make_skill_zip("demo-skill", "1.0.0", files={"a.txt": "a"}))

        result = run("validate", archive)

        assert result.code == 0
        assert result.out.startswith(f"OK: {archive}\n")
        assert "demo-skill" in result.out

    def test_a_wrapped_zip_with_macos_junk_validates_without_the_junk(self, tmp_path: Path):
        archive = tmp_path / "wrapped.zip"
        archive.write_bytes(
            make_skill_zip("demo-skill", "1.0.0", wrapper="demo", macos_junk=True, dir_entries=True)
        )

        report = run("validate", archive, "--json").json()

        assert report["valid"] is True
        assert [item["path"] for item in report["files"]] == ["SKILL.md"]

    def test_validating_a_directory_and_its_packed_zip_gives_the_same_verdict(self, tmp_path: Path):
        source = skill_tree(tmp_path / "pkg")
        archive = tmp_path / "out.zip"
        assert run("pack", source, "-o", archive).code == 0

        from_directory = run("validate", source, "--json").json()
        from_zip = run("validate", archive, "--json").json()

        del from_directory["path"], from_zip["path"]
        assert from_directory == from_zip


def hostile_tree(kind: str, root: Path) -> Path:
    source = skill_tree(root)
    if kind == "symlink":
        os.symlink("/etc/passwd", source / "passwd")
    elif kind == "symlink_to_directory":
        os.symlink(source / "references", source / "alias")
    elif kind == "dangling_symlink":
        os.symlink(source / "nowhere", source / "dangling")
    elif kind == "fifo":
        os.mkfifo(source / "pipe")
    elif kind == "backslash":
        (source / "dir\\file.txt").write_text("x")
    elif kind == "non_utf8":
        try:
            (source / os.fsdecode(b"bad-\xff-name.txt")).write_text("x")
        except OSError:
            pytest.skip("the filesystem refuses non UTF-8 names")
    elif kind == "control_character":
        (source / "line\nbreak.txt").write_text("x")
    elif kind == "sparse_oversized_file":
        with (source / "huge.bin").open("wb") as handle:
            handle.truncate(51 * 1024 * 1024)
    elif kind == "too_many_files":
        for number in range(2001):
            (source / f"f{number}.txt").write_text("x")
    return source


class TestInvalidDirectories:
    @pytest.mark.parametrize(
        ("files", "reason"),
        [
            ({"README.md": "# nothing here\n"}, "MISSING_ENTRYPOINT"),
            ({}, "MISSING_ENTRYPOINT"),
            ({"SKILL.md": skill_md("demo"), "INDEX.md": index_md("demo")}, "AMBIGUOUS_ENTRYPOINT"),
            ({"SKILL.md": "# no frontmatter at all\n"}, "BAD_FRONTMATTER"),
            ({"SKILL.md": "---\nname: [unclosed\n---\n"}, "BAD_FRONTMATTER"),
            (
                {"SKILL.md": "---\nname: demo\nversion: 1.0.0\n---\nbody\n"},
                "MISSING_FIELD",
            ),
            ({"SKILL.md": skill_md("Bad_Name")}, "INVALID_FIELD"),
            ({"SKILL.md": skill_md("demo", description="   ")}, "INVALID_FIELD"),
            ({"SKILL.md": skill_md("demo", tags=["Not Valid!"])}, "INVALID_FIELD"),
            ({"SKILL.md": skill_md("demo", "1.0")}, "INVALID_FIELD"),
            ({"SKILL.md": skill_md("demo", kind="grounding")}, "KIND_MISMATCH"),
            ({"INDEX.md": index_md("demo", kind="skill")}, "KIND_MISMATCH"),
        ],
    )
    def test_package_content_errors_exit_one_with_their_reason(
        self, tmp_path: Path, files: dict[str, str], reason: str
    ):
        source = tmp_path / "pkg"
        source.mkdir()
        write_tree(source, files)

        result = run("validate", source)

        assert result.code == 1
        assert result.err == ""
        lines = result.out.splitlines()
        assert lines[0] == f"INVALID: {source}"
        assert lines[1].startswith(f"  {reason}: ")
        assert len(lines) == 2

    def test_a_version_option_that_disagrees_with_the_frontmatter_is_rejected(self, tmp_path: Path):
        source = skill_tree(tmp_path / "pkg", version="1.0.0")

        result = run("validate", source, "--version", "2.0.0", "--json")

        assert result.code == 1
        assert result.json()["errors"][0]["reason"] == "VERSION_MISMATCH"

    @pytest.mark.parametrize(
        ("kind", "reason"),
        [
            ("symlink", "SYMLINK_NOT_ALLOWED"),
            ("symlink_to_directory", "SYMLINK_NOT_ALLOWED"),
            ("dangling_symlink", "SYMLINK_NOT_ALLOWED"),
            ("fifo", "UNSAFE_PATH"),
            ("backslash", "UNSAFE_PATH"),
            ("non_utf8", "UNSAFE_PATH"),
            ("control_character", "UNSAFE_PATH"),
            ("sparse_oversized_file", "TOO_LARGE"),
            ("too_many_files", "TOO_MANY_FILES"),
        ],
    )
    def test_unpackable_directory_contents_are_reported_not_followed(
        self, tmp_path: Path, kind: str, reason: str
    ):
        source = hostile_tree(kind, tmp_path / "pkg")

        result = run("validate", source, "--json")

        assert result.code == 1
        report = result.json()
        assert report["valid"] is False
        assert [error["reason"] for error in report["errors"]] == [reason]
        assert report["errors"][0]["message"]

    def test_the_symlink_message_names_the_offending_path(self, tmp_path: Path):
        source = hostile_tree("symlink", tmp_path / "pkg")

        result = run("validate", source)

        assert "'passwd' is a symbolic link" in result.out

    def test_a_symlink_inside_an_excluded_directory_is_not_inspected(self, tmp_path: Path):
        source = skill_tree(tmp_path / "pkg")
        (source / "node_modules").mkdir()
        os.symlink("/etc/passwd", source / "node_modules" / "link")

        assert run("validate", source).code == 0

    def test_unreadable_files_fail_without_a_traceback(self, tmp_path: Path):
        if os.geteuid() == 0:
            pytest.skip("root can read everything")
        source = skill_tree(tmp_path / "pkg")
        (source / "references" / "notes.md").chmod(0o000)
        try:
            result = run("validate", source)
        finally:
            (source / "references" / "notes.md").chmod(0o644)

        assert result.code == 1
        assert "cannot read 'references/notes.md'" in result.err
        assert "Traceback" not in result.err + result.out

    def test_entries_that_cannot_be_inspected_fail_without_a_traceback(self, tmp_path: Path):
        if os.geteuid() == 0:
            pytest.skip("root can read everything")
        source = skill_tree(tmp_path / "pkg")
        (source / "references").chmod(0o444)
        try:
            result = run("validate", source)
        finally:
            (source / "references").chmod(0o755)

        assert result.code == 1
        assert "cannot inspect" in result.err
        assert "Traceback" not in result.err + result.out

    def test_an_unreadable_zip_fails_without_a_traceback(self, tmp_path: Path):
        if os.geteuid() == 0:
            pytest.skip("root can read everything")
        archive = tmp_path / "locked.zip"
        archive.write_bytes(make_skill_zip())
        archive.chmod(0o000)
        try:
            result = run("validate", archive)
        finally:
            archive.chmod(0o644)

        assert result.code == 1
        assert result.err.startswith(f"error: cannot read '{archive}'")

    def test_unreadable_directories_fail_without_a_traceback(self, tmp_path: Path):
        if os.geteuid() == 0:
            pytest.skip("root can read everything")
        source = skill_tree(tmp_path / "pkg")
        (source / "references").chmod(0o000)
        try:
            result = run("validate", source)
        finally:
            (source / "references").chmod(0o755)

        assert result.code == 1
        assert "cannot read directory" in result.err
        assert "Traceback" not in result.err + result.out

    def test_an_invalid_package_prints_nothing_on_stderr_in_json_mode(self, tmp_path: Path):
        source = write_tree(tmp_path / "pkg", {"README.md": "x"})

        result = run("validate", source, "--json")

        assert result.err == ""
        assert result.json() == {
            "valid": False,
            "path": str(source),
            "errors": [
                {"reason": "MISSING_ENTRYPOINT", "message": result.json()["errors"][0]["message"]}
            ],
        }


class TestInvalidZips:
    @pytest.mark.parametrize(
        ("data", "reason"),
        [
            (b"this is not a zip archive at all", "NOT_A_ZIP"),
            (b"", "NOT_A_ZIP"),
            (make_skill_zip()[:-30], "CORRUPT_ARCHIVE"),
            (corrupt_entry_data(make_skill_zip(), "SKILL.md"), "CORRUPT_ARCHIVE"),
            (make_encrypted_zip(), "ENCRYPTED_ENTRY"),
            (make_traversal_zip(), "UNSAFE_PATH"),
            (make_traversal_zip("/etc/cron.d/evil"), "UNSAFE_PATH"),
            (make_traversal_zip("sub/../../evil"), "UNSAFE_PATH"),
            (make_symlink_zip(), "SYMLINK_NOT_ALLOWED"),
            (set_compression_method(make_skill_zip(), "SKILL.md", 12), "UNSUPPORTED_COMPRESSION"),
            (make_zip({"a.txt": "x"}), "MISSING_ENTRYPOINT"),
            (make_bomb_zip(60 * 1024 * 1024, declared_size=10), "TOO_LARGE"),
        ],
    )
    def test_hostile_or_broken_archives_are_invalid(self, tmp_path: Path, data: bytes, reason: str):
        archive = tmp_path / "bad.zip"
        archive.write_bytes(data)

        result = run("validate", archive, "--json")

        assert result.code == 1
        assert [error["reason"] for error in result.json()["errors"]] == [reason]

    def test_an_archive_beyond_the_size_limit_is_rejected_without_reading_it_all(
        self, tmp_path: Path
    ):
        archive = tmp_path / "huge.zip"
        with archive.open("wb") as handle:
            handle.write(make_skill_zip())
            handle.truncate(26 * 1024 * 1024)

        result = run("validate", archive, "--json")

        assert result.code == 1
        assert result.json()["errors"][0]["reason"] == "TOO_LARGE"

    def test_paths_that_collide_after_case_folding_are_rejected(self, tmp_path: Path):
        archive = tmp_path / "dup.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("SKILL.md", skill_md("demo"))
            handle.writestr("a.txt", "one")
            handle.writestr("A.TXT", "two")

        result = run("validate", archive, "--json")

        assert result.json()["errors"][0]["reason"] == "DUPLICATE_PATH"


class TestUsage:
    def test_a_missing_path_is_a_usage_error(self, tmp_path: Path):
        result = run("validate", tmp_path / "nowhere")

        assert result.code == 2
        assert result.out == ""
        assert "no such file or directory" in result.err

    def test_an_invalid_version_option_is_a_usage_error(self, tmp_path: Path):
        source = skill_tree(tmp_path / "pkg")

        result = run("validate", source, "--version", "banana")

        assert result.code == 2
        assert "invalid --version" in result.err

    @pytest.mark.parametrize("raw", ["", "   "])
    def test_an_empty_path_is_a_usage_error_not_the_current_directory(
        self, raw: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.chdir(skill_tree(tmp_path / "pkg"))

        result = run("validate", raw)

        assert result.code == 2
        assert result.err == "alx validate: error: the path is empty\n"

    def test_the_path_argument_is_required(self):
        result = run("validate")

        assert result.code == 2
        assert "required" in result.err

    def test_a_special_file_as_the_path_is_reported_as_invalid_input(self, tmp_path: Path):
        fifo = tmp_path / "pipe"
        os.mkfifo(fifo)

        result = run("validate", fifo)

        assert result.code == 2
        assert "neither a file nor a directory" in result.err

    def test_grounding_and_skill_samples_from_the_helpers_are_valid(self, tmp_path: Path):
        (tmp_path / "g.zip").write_bytes(make_grounding_zip())
        (tmp_path / "s.zip").write_bytes(make_skill_zip())

        assert run("validate", tmp_path / "g.zip").code == 0
        assert run("validate", tmp_path / "s.zip").code == 0
