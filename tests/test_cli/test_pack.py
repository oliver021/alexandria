from __future__ import annotations

import hashlib
import os
import random
import zipfile
from pathlib import Path

import pytest

from app.cli import pack_directory
from app.services.package_inspector import inspect_package
from tests.helpers.packages import make_skill_zip, skill_md
from tests.test_cli.support import parse_fields, run, skill_tree, write_tree

pytestmark = pytest.mark.skipif(os.name == "nt", reason="relies on POSIX file semantics")

TRICKY_FILES = {
    "a.txt": "a",
    "a-b.txt": "dash",
    "a_b.txt": "underscore",
    "B.txt": "upper",
    "a/b.txt": "nested",
    "a/c/d.txt": "deeper",
    "z/last.md": "last",
    "é.txt": "accent",
    "data/blob.bin": bytes(range(256)) * 4,
    "empty.txt": "",
}


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_tree(root: Path, order: list[str], *, noise: int = 0) -> Path:
    files: dict[str, str | bytes] = {"SKILL.md": skill_md("order-skill", "1.0.0"), **TRICKY_FILES}
    for relative in order:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        content = files[relative]
        target.write_bytes(content.encode() if isinstance(content, str) else content)
        if noise:
            os.utime(target, (1_000_000 * noise, 2_000_000 * noise))
            target.chmod(0o600 if noise % 2 else 0o664)
    return root


class TestOutput:
    def test_default_output_is_name_version_zip_in_the_working_directory(
        self, tmp_path: Path, workdir: Path
    ):
        source = skill_tree(tmp_path / "pkg", "demo-skill", "1.4.0")

        result = run("pack", source)

        assert result.code == 0
        assert [item.name for item in workdir.iterdir()] == ["demo-skill-1.4.0.zip"]
        assert result.out.splitlines()[0] == "Packed demo-skill-1.4.0.zip"

    def test_the_output_option_names_the_file(self, tmp_path: Path):
        source = skill_tree(tmp_path / "pkg")
        output = tmp_path / "out" / "nested" / "custom.zip"

        result = run("pack", source, "-o", output)

        assert result.code == 0
        assert zipfile.is_zipfile(output)
        assert result.out.splitlines()[0] == f"Packed {output}"

    def test_the_output_option_accepts_an_existing_directory(self, tmp_path: Path):
        source = skill_tree(tmp_path / "pkg", "demo-skill", "1.0.0")
        destination = tmp_path / "dist"
        destination.mkdir()

        result = run("pack", source, "--output", destination)

        assert result.code == 0
        assert (destination / "demo-skill-1.0.0.zip").is_file()

    def test_a_trailing_slash_makes_the_output_a_directory_to_create(self, tmp_path: Path):
        source = skill_tree(tmp_path / "pkg", "demo-skill", "1.0.0")

        result = run("pack", source, "-o", f"{tmp_path}/fresh/dir/")

        assert result.code == 0
        assert (tmp_path / "fresh" / "dir" / "demo-skill-1.0.0.zip").is_file()

    def test_the_human_report_describes_the_package(self, tmp_path: Path):
        source = skill_tree(tmp_path / "pkg", "demo-skill", "1.0.0")
        output = tmp_path / "o.zip"

        result = run("pack", source, "-o", output)

        fields = parse_fields(result.out.splitlines()[1:])
        assert fields["kind"] == "skill"
        assert fields["name"] == "demo-skill"
        assert fields["version"] == "1.0.0"
        assert fields["files"].startswith("3 (")
        assert fields["sha256"] == sha256_of(output)
        assert fields["size"].endswith(f"({output.stat().st_size} bytes)")

    def test_json_output_describes_the_package(self, tmp_path: Path):
        source = skill_tree(tmp_path / "pkg", "demo-skill", "1.0.0", tags=["t"])
        output = tmp_path / "o.zip"

        result = run("pack", source, "-o", output, "--json")

        assert result.code == 0
        assert result.err == ""
        report = result.json()
        assert report["path"] == str(output)
        assert report["sha256"] == sha256_of(output)
        assert report["size_bytes"] == output.stat().st_size
        assert (report["kind"], report["name"], report["version"]) == (
            "skill",
            "demo-skill",
            "1.0.0",
        )
        assert report["file_count"] == 3
        assert report["tags"] == ["t"]

    def test_the_written_archive_is_a_valid_package(self, tmp_path: Path):
        source = skill_tree(tmp_path / "pkg")
        output = tmp_path / "o.zip"
        run("pack", source, "-o", output)

        parsed = inspect_package(output.read_bytes())

        assert (parsed.name, parsed.version, parsed.file_count) == ("demo-skill", "1.0.0", 3)
        assert parsed.root_prefix == ""

    def test_the_version_option_completes_a_package_without_frontmatter_version(
        self, tmp_path: Path
    ):
        source = write_tree(
            tmp_path / "pkg", {"SKILL.md": "---\nname: no-version\ndescription: d\n---\nb\n"}
        )

        result = run("pack", source, "-o", tmp_path / "o.zip", "--version", "0.2.0", "--json")

        assert result.code == 0
        assert result.json()["version"] == "0.2.0"

    def test_no_temporary_files_are_left_next_to_the_output(self, tmp_path: Path):
        source = skill_tree(tmp_path / "pkg")
        destination = tmp_path / "dist"

        run("pack", source, "-o", destination / "o.zip")

        assert [item.name for item in destination.iterdir()] == ["o.zip"]

    def test_an_existing_output_is_replaced_atomically(self, tmp_path: Path):
        source = skill_tree(tmp_path / "pkg")
        output = tmp_path / "o.zip"
        output.write_bytes(b"old content")

        result = run("pack", source, "-o", output)

        assert result.code == 0
        assert zipfile.is_zipfile(output)


class TestDeterminism:
    def test_packing_the_same_tree_twice_gives_the_same_bytes(self, tmp_path: Path):
        source = skill_tree(tmp_path / "pkg", files={"data/x.bin": os.urandom(5000)})

        first = run("pack", source, "-o", tmp_path / "one.zip", "--json").json()
        second = run("pack", source, "-o", tmp_path / "two.zip", "--json").json()

        assert first["sha256"] == second["sha256"]
        assert (tmp_path / "one.zip").read_bytes() == (tmp_path / "two.zip").read_bytes()
        assert sha256_of(tmp_path / "one.zip") == first["sha256"]

    @pytest.mark.parametrize("seed", [1, 2, 3, 4, 5, 6])
    def test_the_creation_order_of_the_files_does_not_change_the_package(
        self, tmp_path: Path, seed: int
    ):
        names = ["SKILL.md", *TRICKY_FILES]
        reference = build_tree(tmp_path / "reference", names)
        shuffled = names[:]
        random.Random(seed).shuffle(shuffled)
        other = build_tree(tmp_path / "other", shuffled)
        reverse = build_tree(tmp_path / "reverse", names[::-1])

        digests = {
            run("pack", tree, "-o", tmp_path / f"{tree.name}.zip", "--json").json()["sha256"]
            for tree in (reference, other, reverse)
        }

        assert len(digests) == 1

    def test_modification_times_and_permission_noise_do_not_change_the_package(
        self, tmp_path: Path
    ):
        names = ["SKILL.md", *TRICKY_FILES]
        quiet = build_tree(tmp_path / "quiet", names)
        noisy_one = build_tree(tmp_path / "noisy1", names, noise=1)
        noisy_two = build_tree(tmp_path / "noisy2", names[::-1], noise=2)

        digests = {
            run("pack", tree, "-o", tmp_path / f"{tree.name}.zip", "--json").json()["sha256"]
            for tree in (quiet, noisy_one, noisy_two)
        }

        assert len(digests) == 1

    def test_the_location_of_the_tree_does_not_change_the_package(self, tmp_path: Path):
        names = ["SKILL.md", *TRICKY_FILES]
        here = build_tree(tmp_path / "here", names)
        deeper = build_tree(tmp_path / "a" / "much" / "deeper" / "place", names)

        digests = {
            run("pack", tree, "-o", tmp_path / f"{index}.zip", "--json").json()["sha256"]
            for index, tree in enumerate((here, deeper))
        }

        assert len(digests) == 1

    def test_changing_a_byte_or_a_name_changes_the_package(self, tmp_path: Path):
        names = ["SKILL.md", *TRICKY_FILES]
        base = build_tree(tmp_path / "base", names)
        edited = build_tree(tmp_path / "edited", names)
        (edited / "a.txt").write_text("b")
        renamed = build_tree(tmp_path / "renamed", names)
        (renamed / "a.txt").rename(renamed / "c.txt")

        digests = {
            run("pack", tree, "-o", tmp_path / f"{tree.name}.zip", "--json").json()["sha256"]
            for tree in (base, edited, renamed)
        }

        assert len(digests) == 3

    def test_the_archive_has_normalised_metadata(self, tmp_path: Path):
        source = build_tree(tmp_path / "pkg", ["SKILL.md", *TRICKY_FILES], noise=3)
        output = tmp_path / "o.zip"
        run("pack", source, "-o", output)

        with zipfile.ZipFile(output) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            assert names == sorted(names)
            assert names == sorted(["SKILL.md", *TRICKY_FILES])
            assert archive.comment == b""
            for info in infos:
                assert info.date_time == (1980, 1, 1, 0, 0, 0)
                assert info.create_system == 3
                assert info.extra == b""
                assert info.comment == b""
                assert info.compress_type == zipfile.ZIP_DEFLATED
                assert info.flag_bits & 0x1 == 0
                assert info.flag_bits & 0x8 == 0
                assert not info.is_dir()
                assert info.external_attr >> 16 == 0o100644
                assert info.external_attr & 0xFFFF == 0

    def test_the_zip_entries_follow_the_manifest_order(self, tmp_path: Path):
        source = build_tree(tmp_path / "pkg", ["SKILL.md", *TRICKY_FILES])
        output = tmp_path / "o.zip"
        run("pack", source, "-o", output)

        parsed = inspect_package(output.read_bytes())

        with zipfile.ZipFile(output) as archive:
            assert archive.namelist() == [entry.path for entry in parsed.manifest]

    @pytest.mark.parametrize(
        ("mode", "expected"),
        [
            (0o755, 0o755),
            (0o700, 0o755),
            (0o750, 0o755),
            (0o775, 0o755),
            (0o777, 0o755),
            (0o4755, 0o755),
            (0o644, 0o644),
            (0o600, 0o644),
            (0o640, 0o644),
            (0o664, 0o644),
            (0o666, 0o644),
            (0o444, 0o644),
        ],
    )
    def test_permissions_are_normalised_by_the_owner_executable_bit(
        self, tmp_path: Path, mode: int, expected: int
    ):
        source = skill_tree(tmp_path / "pkg", files={"tool": "x"})
        (source / "tool").chmod(mode)
        output = tmp_path / "o.zip"
        run("pack", source, "-o", output)

        with zipfile.ZipFile(output) as archive:
            attributes = archive.getinfo("tool").external_attr >> 16

        assert attributes == 0o100000 | expected

    def test_the_standard_skill_script_keeps_its_executable_bit(self, tmp_path: Path):
        source = skill_tree(tmp_path / "pkg")
        output = tmp_path / "o.zip"
        run("pack", source, "-o", output)

        with zipfile.ZipFile(output) as archive:
            assert archive.getinfo("scripts/run.sh").external_attr >> 16 == 0o100755
            assert archive.getinfo("SKILL.md").external_attr >> 16 == 0o100644


class TestExclusions:
    def test_junk_is_not_packed(self, tmp_path: Path):
        source = skill_tree(tmp_path / "pkg")
        write_tree(
            source,
            {
                ".git/config": "x",
                ".git/objects/ab/cdef": "x",
                "__pycache__/mod.cpython-314.pyc": "x",
                "scripts/__pycache__/x.pyc": "x",
                "scripts/compiled.pyc": "x",
                "node_modules/pkg/index.js": "x",
                "references/node_modules/inner.js": "x",
                ".DS_Store": "x",
                "references/.DS_Store": "x",
                "Thumbs.db": "x",
            },
        )
        output = tmp_path / "o.zip"

        result = run("pack", source, "-o", output)

        assert result.code == 0
        with zipfile.ZipFile(output) as archive:
            assert archive.namelist() == ["SKILL.md", "references/notes.md", "scripts/run.sh"]

    def test_hidden_files_that_are_not_junk_are_packed(self, tmp_path: Path):
        source = skill_tree(tmp_path / "pkg", files={".env.example": "KEY=\n", ".hidden/x": "y"})
        output = tmp_path / "o.zip"
        run("pack", source, "-o", output)

        with zipfile.ZipFile(output) as archive:
            assert ".env.example" in archive.namelist()
            assert ".hidden/x" in archive.namelist()

    def test_empty_directories_are_not_represented(self, tmp_path: Path):
        source = skill_tree(tmp_path / "pkg")
        (source / "empty" / "deeper").mkdir(parents=True)
        output = tmp_path / "o.zip"
        run("pack", source, "-o", output)

        with zipfile.ZipFile(output) as archive:
            assert not any(name.endswith("/") for name in archive.namelist())
            assert all("empty" not in name for name in archive.namelist())


class TestOutputInsideTheTree:
    def test_packing_from_inside_the_directory_is_idempotent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        source = skill_tree(tmp_path / "pkg", "demo-skill", "1.0.0")
        monkeypatch.chdir(source)

        first = run("pack", ".", "--json")
        second = run("pack", ".", "--json")

        assert first.code == second.code == 0
        assert first.json()["sha256"] == second.json()["sha256"]
        assert first.json()["file_count"] == second.json()["file_count"] == 3
        assert (source / "demo-skill-1.0.0.zip").is_file()
        assert first.err == second.err == ""

    def test_the_default_artifact_is_never_part_of_the_package(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        source = skill_tree(tmp_path / "pkg", "demo-skill", "1.0.0")
        monkeypatch.chdir(source)
        run("pack", ".")

        validated = run("validate", ".", "--json").json()

        assert validated["file_count"] == 3
        assert "demo-skill-1.0.0.zip" not in [item["path"] for item in validated["files"]]
        sha = run("pack", ".", "-o", tmp_path / "elsewhere.zip", "--json").json()["sha256"]
        assert validated["sha256"] == sha

    def test_a_custom_output_inside_the_tree_is_excluded_and_warned_about(self, tmp_path: Path):
        source = skill_tree(tmp_path / "pkg")
        output = source / "build" / "custom.zip"

        first = run("pack", source, "-o", output, "--json")
        second = run("pack", source, "-o", output, "--json")

        assert first.json()["sha256"] == second.json()["sha256"]
        assert first.json()["file_count"] == 3
        assert "inside the package directory" in first.err
        assert "inside the package directory" in second.err

    def test_an_output_directory_inside_the_tree_is_excluded_on_the_second_run(
        self, tmp_path: Path
    ):
        source = skill_tree(tmp_path / "pkg")

        first = run("pack", source, "-o", f"{source}/dist/", "--json")
        second = run("pack", source, "-o", f"{source}/dist/", "--json")

        assert first.json()["sha256"] == second.json()["sha256"]
        assert second.json()["file_count"] == 3

    @pytest.mark.parametrize("target", ["references/notes.md", "SKILL.md", "scripts/run.sh"])
    def test_a_source_file_is_never_overwritten_by_the_archive(self, tmp_path: Path, target: str):
        source = skill_tree(tmp_path / "pkg")
        before = (source / target).read_bytes()

        result = run("pack", source, "-o", source / target)

        assert result.code == 1
        assert result.err.startswith("error: refusing to overwrite ")
        assert "part of the package" in result.err
        assert (source / target).read_bytes() == before

    def test_an_unreadable_file_inside_the_tree_is_not_taken_for_an_archive(self, tmp_path: Path):
        if os.geteuid() == 0:
            pytest.skip("root can read everything")
        source = skill_tree(tmp_path / "pkg", files={"locked.zip": b"PK\x03\x04 pretend"})
        locked = source / "locked.zip"
        locked.chmod(0o000)
        try:
            result = run("pack", source, "-o", locked)
        finally:
            locked.chmod(0o644)

        assert result.code == 1
        assert result.err.startswith("error: refusing to overwrite ")

    def test_a_previous_archive_inside_the_tree_may_be_replaced(self, tmp_path: Path):
        source = skill_tree(tmp_path / "pkg")
        output = source / "old.zip"
        output.write_bytes(make_skill_zip("old-one", "0.1.0"))

        result = run("pack", source, "-o", output)

        assert result.code == 0
        assert inspect_package(output.read_bytes()).name == "demo-skill"

    def test_an_output_outside_the_tree_gives_no_warning(self, tmp_path: Path):
        source = skill_tree(tmp_path / "pkg")

        result = run("pack", source, "-o", tmp_path / "outside.zip")

        assert result.err == ""


class TestRefusals:
    def test_an_invalid_package_exits_one_and_writes_nothing(self, tmp_path: Path, workdir: Path):
        source = write_tree(tmp_path / "pkg", {"README.md": "no entrypoint"})

        result = run("pack", source)

        assert result.code == 1
        assert "MISSING_ENTRYPOINT" in result.err
        assert result.err.startswith("error: INVALID_PACKAGE (MISSING_ENTRYPOINT): ")
        assert list(workdir.iterdir()) == []

    def test_an_invalid_package_does_not_create_the_output_directories(self, tmp_path: Path):
        source = write_tree(tmp_path / "pkg", {"SKILL.md": "# no frontmatter"})
        output = tmp_path / "new" / "nested" / "o.zip"

        result = run("pack", source, "-o", output)

        assert result.code == 1
        assert not (tmp_path / "new").exists()

    def test_an_invalid_package_leaves_an_existing_output_untouched(self, tmp_path: Path):
        source = write_tree(tmp_path / "pkg", {"SKILL.md": "# no frontmatter"})
        output = tmp_path / "o.zip"
        output.write_bytes(b"precious")

        result = run("pack", source, "-o", output)

        assert result.code == 1
        assert output.read_bytes() == b"precious"
        assert [item.name for item in tmp_path.iterdir() if item.name != "pkg"] == ["o.zip"]

    def test_json_mode_reports_the_failure_as_json_on_stderr(self, tmp_path: Path):
        source = write_tree(tmp_path / "pkg", {"SKILL.md": "# no frontmatter"})

        result = run("pack", source, "-o", tmp_path / "o.zip", "--json")

        assert result.code == 1
        assert result.out == ""
        error = result.error_json()["error"]
        assert error["code"] == "INVALID_PACKAGE"
        assert error["reason"] == "BAD_FRONTMATTER"
        assert error["message"]

    def test_a_symlink_in_the_tree_aborts_the_pack(self, tmp_path: Path):
        source = skill_tree(tmp_path / "pkg")
        os.symlink("/etc/passwd", source / "secrets")
        output = tmp_path / "o.zip"

        result = run("pack", source, "-o", output)

        assert result.code == 1
        assert "SYMLINK_NOT_ALLOWED" in result.err
        assert not output.exists()

    def test_a_file_is_not_a_directory_to_pack(self, tmp_path: Path):
        archive = tmp_path / "a.zip"
        archive.write_bytes(b"x")

        result = run("pack", archive)

        assert result.code == 2
        assert "is not a directory" in result.err

    def test_an_empty_directory_argument_is_a_usage_error(self):
        result = run("pack", "")

        assert result.code == 2
        assert result.err == "alx pack: error: the directory is empty\n"

    def test_a_missing_directory_is_a_usage_error(self, tmp_path: Path):
        result = run("pack", tmp_path / "nowhere")

        assert result.code == 2

    def test_an_output_that_is_a_file_in_the_way_is_reported_without_a_traceback(
        self, tmp_path: Path
    ):
        source = skill_tree(tmp_path / "pkg")
        blocker = tmp_path / "blocker"
        blocker.write_text("i am a file")

        result = run("pack", source, "-o", blocker / "o.zip")

        assert result.code == 1
        assert result.err.startswith("error: ")
        assert "Traceback" not in result.err

    def test_an_invalid_version_option_is_a_usage_error(self, tmp_path: Path):
        source = skill_tree(tmp_path / "pkg")

        assert run("pack", source, "--version", "1.0").code == 2

    def test_packing_is_consistent_with_the_library_function(self, tmp_path: Path):
        source = skill_tree(tmp_path / "pkg")
        output = tmp_path / "o.zip"
        run("pack", source, "-o", output)

        assert output.read_bytes() == pack_directory(source).data
