from __future__ import annotations

import errno
import hashlib
import os
import uuid
from pathlib import Path

import pytest

from app import cli
from app.cli import pack_directory
from tests.helpers.packages import (
    make_bomb_zip,
    make_encrypted_zip,
    make_grounding_zip,
    make_raw_package,
    make_skill_zip,
    make_symlink_zip,
    make_traversal_zip,
    stored_entry,
)
from tests.test_cli.support import (
    FakeRegistry,
    parse_fields,
    remote_env,
    run,
    skill_tree,
    tree_snapshot,
    write_tree,
)

pytestmark = pytest.mark.skipif(os.name == "nt", reason="relies on POSIX file semantics")

NAME = "pull-skill"
BLOB = bytes(range(256)) * 8


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def source(tmp_path: Path) -> Path:
    return skill_tree(
        tmp_path / "src",
        NAME,
        "1.0.0",
        files={
            "data/blob.bin": BLOB,
            "docs/empty.txt": "",
            "naïve/ünï.txt": "unicode content\n",
            "deep/er/and/deeper/file.txt": "deep\n",
        },
    )


@pytest.fixture
def served(registry: FakeRegistry, source: Path) -> FakeRegistry:
    registry.add_package(pack_directory(source).data)
    return registry


def pull(registry: FakeRegistry, *argv: object, env: dict[str, str] | None = None):
    return run("pull", *argv, transport=registry.transport, env=env if env is not None else {})


def all_names(root: Path) -> list[str]:
    return sorted(
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file() or path.is_symlink()
    )


def find_anywhere(root: Path, fragment: str) -> list[Path]:
    return [path for path in root.rglob("*") if fragment in path.name]


class TestExtraction:
    def test_the_package_files_are_extracted_exactly(
        self, served: FakeRegistry, source: Path, tmp_path: Path
    ):
        destination = tmp_path / "out"

        result = pull(served, NAME, "-d", destination)

        assert result.code == 0
        assert result.err == ""
        assert tree_snapshot(destination) == tree_snapshot(source)

    def test_permissions_are_zero_six_four_four_or_zero_seven_five_five(
        self, served: FakeRegistry, tmp_path: Path
    ):
        destination = tmp_path / "out"
        pull(served, NAME, "-d", destination)

        modes = {
            str(path.relative_to(destination)): path.stat().st_mode & 0o7777
            for path in destination.rglob("*")
            if path.is_file()
        }

        assert modes["scripts/run.sh"] == 0o755
        assert {mode for name, mode in modes.items() if name != "scripts/run.sh"} == {0o644}

    def test_permissions_ignore_the_umask(self, served: FakeRegistry, tmp_path: Path):
        previous = os.umask(0o077)
        try:
            pull(served, NAME, "-d", tmp_path / "out")
        finally:
            os.umask(previous)

        assert (tmp_path / "out" / "SKILL.md").stat().st_mode & 0o777 == 0o644
        assert (tmp_path / "out" / "scripts" / "run.sh").stat().st_mode & 0o777 == 0o755

    def test_the_report_lists_the_extracted_files(self, served: FakeRegistry, tmp_path: Path):
        destination = tmp_path / "out"

        result = pull(served, NAME, "-d", destination)

        lines = result.out.splitlines()
        assert lines[0] == f"Pulled {NAME} 1.0.0 (skill) into {destination}"
        fields = parse_fields(lines[1:3])
        assert fields["files"].startswith("7 (")
        assert fields["sha256"] == sha256(pack_directory(tmp_path / "src").data)
        assert [line.strip() for line in lines[3:]] == [
            "SKILL.md",
            "data/blob.bin",
            "deep/er/and/deeper/file.txt",
            "docs/empty.txt",
            "naïve/ünï.txt",
            "references/notes.md",
            "scripts/run.sh",
        ]
        assert [line.strip() for line in lines[3:]] == all_names(destination)

    def test_the_destination_defaults_to_a_directory_named_after_the_package(
        self, served: FakeRegistry, workdir: Path
    ):
        result = pull(served, NAME)

        assert result.code == 0
        assert (workdir / NAME / "SKILL.md").is_file()
        assert f"into {NAME}\n" in result.out

    def test_a_version_can_be_pinned(self, registry: FakeRegistry, tmp_path: Path):
        registry.add_package(make_skill_zip(NAME, "1.0.0", files={"v.txt": "old"}))
        registry.add_package(make_skill_zip(NAME, "1.1.0", files={"v.txt": "new"}))

        old = pull(registry, f"{NAME}@1.0.0", "-d", tmp_path / "old")
        latest = pull(registry, NAME, "-d", tmp_path / "latest")
        explicit = pull(registry, f"{NAME}@latest", "-d", tmp_path / "explicit")

        assert (old.code, latest.code, explicit.code) == (0, 0, 0)
        assert (tmp_path / "old" / "v.txt").read_text() == "old"
        assert (tmp_path / "latest" / "v.txt").read_text() == "new"
        assert (tmp_path / "explicit" / "v.txt").read_text() == "new"
        assert "1.0.0" in old.out.splitlines()[0]

    def test_a_grounding_package_with_binary_data_keeps_every_byte(
        self, registry: FakeRegistry, tmp_path: Path
    ):
        registry.add_package(
            make_grounding_zip("facts", "1.0.0", files={"data/db.sqlite": BLOB, "t/a.csv": "a,b\n"})
        )

        result = pull(registry, "facts", "-d", tmp_path / "out")

        assert result.code == 0
        assert (tmp_path / "out" / "data" / "db.sqlite").read_bytes() == BLOB
        assert (tmp_path / "out" / "INDEX.md").is_file()
        assert "(grounding)" in result.out

    def test_files_are_taken_relative_to_the_wrapper_directory(
        self, registry: FakeRegistry, tmp_path: Path
    ):
        registry.add_package(
            make_skill_zip(NAME, "1.0.0", wrapper="pull-skill-1.0.0", files={"a/b.txt": "x"})
        )

        pull(registry, NAME, "-d", tmp_path / "out")

        assert all_names(tmp_path / "out") == ["SKILL.md", "a/b.txt"]

    def test_operating_system_junk_is_not_extracted(self, registry: FakeRegistry, tmp_path: Path):
        registry.add_package(
            make_skill_zip(NAME, "1.0.0", wrapper="pkg", macos_junk=True, dir_entries=True)
        )

        pull(registry, NAME, "-d", tmp_path / "out")

        assert all_names(tmp_path / "out") == ["SKILL.md"]
        assert [path.name for path in (tmp_path / "out").iterdir()] == ["SKILL.md"]

    def test_the_destination_is_created_with_its_parents(
        self, served: FakeRegistry, tmp_path: Path
    ):
        destination = tmp_path / "a" / "b" / "c"

        assert pull(served, NAME, "-d", destination).code == 0
        assert (destination / "SKILL.md").is_file()

    def test_other_files_in_an_existing_destination_are_left_alone(
        self, served: FakeRegistry, tmp_path: Path
    ):
        destination = write_tree(
            tmp_path / "out", {"mine.txt": "keep me", "scripts/mine.sh": "keep too"}
        )

        result = pull(served, NAME, "-d", destination)

        assert result.code == 0
        assert (destination / "mine.txt").read_text() == "keep me"
        assert (destination / "scripts" / "mine.sh").read_text() == "keep too"
        assert (destination / "scripts" / "run.sh").is_file()

    def test_json_output(self, served: FakeRegistry, tmp_path: Path):
        destination = tmp_path / "out"

        result = pull(served, NAME, "-d", destination, "--json")

        assert result.code == 0
        assert result.err == ""
        report = result.json()
        assert report["name"] == NAME
        assert report["version"] == "1.0.0"
        assert report["kind"] == "skill"
        assert report["directory"] == str(destination)
        assert report["files"] == sorted(all_names(destination))
        assert report["sha256"] == served.entries[NAME].versions["1.0.0"].parsed.sha256

    def test_a_long_listing_is_truncated_in_the_report_only(
        self, registry: FakeRegistry, tmp_path: Path
    ):
        files = {f"f/{number:03}.txt": str(number) for number in range(120)}
        registry.add_package(make_skill_zip(NAME, "1.0.0", files=files))

        human = pull(registry, NAME, "-d", tmp_path / "a")
        machine = pull(registry, NAME, "-d", tmp_path / "b", "--json")

        assert human.out.splitlines()[-1] == "    ... and 71 more"
        assert len(machine.json()["files"]) == 121
        assert len(all_names(tmp_path / "a")) == 121

    def test_a_package_with_many_files_is_extracted_completely(
        self, registry: FakeRegistry, tmp_path: Path
    ):
        files = {
            f"d{number % 17}/s{number % 5}/f{number}.txt": f"c{number}" for number in range(1500)
        }
        registry.add_package(make_skill_zip(NAME, "1.0.0", files=files))

        result = pull(registry, NAME, "-d", tmp_path / "out")

        assert result.code == 0
        assert len(all_names(tmp_path / "out")) == 1501
        assert (tmp_path / "out" / "d9" / "s3" / "f1233.txt").read_text() == "c1233"

    def test_a_token_is_used_for_private_registries(self, served: FakeRegistry, tmp_path: Path):
        served.public_read = False

        denied = pull(served, NAME, "-d", tmp_path / "a")
        allowed = pull(served, NAME, "-d", tmp_path / "b", env=remote_env("alx_read"))

        assert denied.code == 1
        assert not (tmp_path / "a").exists()
        assert allowed.code == 0

    def test_pulling_by_id_uses_the_declared_name_for_the_default_directory(
        self, served: FakeRegistry, workdir: Path
    ):
        result = pull(served, served.entries[NAME].skill_id)

        assert result.code == 0
        assert (workdir / NAME / "SKILL.md").is_file()

    def test_no_temporary_files_remain(self, served: FakeRegistry, tmp_path: Path):
        pull(served, NAME, "-d", tmp_path / "out")

        assert not find_anywhere(tmp_path / "out", ".alx-")


class TestRoundTrip:
    def test_pack_publish_pull_reproduces_the_tree_and_the_package(
        self, registry: FakeRegistry, source: Path, tmp_path: Path
    ):
        published = run(
            "publish", source, "--publish", transport=registry.transport, env=remote_env()
        )
        pulled = pull(registry, NAME, "-d", tmp_path / "out")

        assert (published.code, pulled.code) == (0, 0)
        assert tree_snapshot(tmp_path / "out") == tree_snapshot(source)
        repacked = run("pack", tmp_path / "out", "-o", tmp_path / "again.zip", "--json").json()
        original = run("pack", source, "-o", tmp_path / "orig.zip", "--json").json()
        assert repacked["sha256"] == original["sha256"]

    def test_pulling_twice_needs_force_and_is_then_idempotent(
        self, served: FakeRegistry, tmp_path: Path
    ):
        destination = tmp_path / "out"
        pull(served, NAME, "-d", destination)
        before = tree_snapshot(destination)

        again = pull(served, NAME, "-d", destination)
        forced = pull(served, NAME, "-d", destination, "--force")

        assert again.code == 1
        assert forced.code == 0
        assert tree_snapshot(destination) == before


class TestOverwriting:
    def test_existing_files_are_never_overwritten_without_force(
        self, served: FakeRegistry, tmp_path: Path
    ):
        destination = write_tree(tmp_path / "out", {"SKILL.md": "my own skill", "keep.txt": "k"})
        before = tree_snapshot(destination)

        result = pull(served, NAME, "-d", destination)

        assert result.code == 1
        assert result.out == ""
        assert result.err.startswith("error: CONFLICT: 1 file(s) already exist: 'SKILL.md'")
        assert "--force" in result.err
        assert tree_snapshot(destination) == before

    def test_a_conflict_writes_nothing_at_all(self, served: FakeRegistry, tmp_path: Path):
        destination = write_tree(tmp_path / "out", {"references/notes.md": "mine"})
        before = tree_snapshot(destination)

        result = pull(served, NAME, "-d", destination)

        assert result.code == 1
        assert tree_snapshot(destination) == before
        assert not (destination / "SKILL.md").exists()
        assert not (destination / "scripts").exists()

    def test_every_conflict_is_listed_up_to_a_limit(self, registry: FakeRegistry, tmp_path: Path):
        files = {f"f{number}.txt": "new" for number in range(9)}
        registry.add_package(make_skill_zip(NAME, "1.0.0", files=files))
        destination = write_tree(tmp_path / "out", {name: "old" for name in files})

        result = pull(registry, NAME, "-d", destination)

        assert result.code == 1
        assert result.err.count("'f") == 5
        assert "9 file(s) already exist" in result.err
        assert "and 4 more" in result.err

    def test_force_replaces_the_conflicting_files_and_nothing_else(
        self, served: FakeRegistry, source: Path, tmp_path: Path
    ):
        destination = write_tree(
            tmp_path / "out",
            {"SKILL.md": "mine", "scripts/run.sh": "mine", "unrelated.txt": "untouched"},
        )
        (destination / "scripts" / "run.sh").chmod(0o444)

        result = pull(served, NAME, "-d", destination, "--force")

        assert result.code == 0
        assert (destination / "SKILL.md").read_bytes() == (source / "SKILL.md").read_bytes()
        assert (destination / "scripts" / "run.sh").stat().st_mode & 0o777 == 0o755
        assert (destination / "unrelated.txt").read_text() == "untouched"
        assert tree_snapshot(destination).keys() == {*tree_snapshot(source).keys(), "unrelated.txt"}

    def test_a_conflict_with_a_destination_that_is_a_file_is_refused(
        self, served: FakeRegistry, tmp_path: Path
    ):
        destination = tmp_path / "out"
        destination.write_text("i am a file")

        result = pull(served, NAME, "-d", destination, "--force")

        assert result.code == 1
        assert "exists and is not a directory" in result.err
        assert destination.read_text() == "i am a file"

    def test_a_parent_of_the_destination_that_is_a_file_is_reported(
        self, served: FakeRegistry, tmp_path: Path
    ):
        blocker = tmp_path / "blocker"
        blocker.write_text("file")

        result = pull(served, NAME, "-d", blocker / "out")

        assert result.code == 1
        assert result.err.startswith("error: ")
        assert "Traceback" not in result.err
        assert blocker.read_text() == "file"


class TestSymbolicLinks:
    @pytest.fixture
    def outside(self, tmp_path: Path) -> Path:
        directory = tmp_path / "outside"
        directory.mkdir()
        (directory / "precious.txt").write_text("precious")
        return directory

    @pytest.mark.parametrize("force", [False, True])
    def test_a_symlinked_directory_in_the_destination_is_never_written_through(
        self, served: FakeRegistry, tmp_path: Path, outside: Path, force: bool
    ):
        destination = tmp_path / "out"
        destination.mkdir()
        os.symlink(outside, destination / "scripts")
        argv = ["--force"] if force else []

        result = pull(served, NAME, "-d", destination, *argv)

        assert result.code == 1
        assert "refusing to write through the symbolic link 'scripts'" in result.err
        assert all_names(outside) == ["precious.txt"]
        assert all_names(destination) == ["scripts"]

    @pytest.mark.parametrize("force", [False, True])
    def test_a_symlinked_file_in_the_destination_is_never_replaced_or_followed(
        self, served: FakeRegistry, tmp_path: Path, outside: Path, force: bool
    ):
        destination = tmp_path / "out"
        destination.mkdir()
        os.symlink(outside / "precious.txt", destination / "SKILL.md")
        argv = ["--force"] if force else []

        result = pull(served, NAME, "-d", destination, *argv)

        assert result.code == 1
        assert (outside / "precious.txt").read_text() == "precious"
        assert (destination / "SKILL.md").is_symlink()
        assert all_names(destination) == ["SKILL.md"]
        if force:
            assert "refusing to replace the symbolic link 'SKILL.md'" in result.err

    def test_a_dangling_symlink_at_a_target_path_is_refused(
        self, served: FakeRegistry, tmp_path: Path
    ):
        destination = tmp_path / "out"
        destination.mkdir()
        os.symlink(tmp_path / "does-not-exist", destination / "SKILL.md")

        result = pull(served, NAME, "-d", destination, "--force")

        assert result.code == 1
        assert not (tmp_path / "does-not-exist").exists()

    def test_a_deep_symlinked_component_is_refused(
        self, served: FakeRegistry, tmp_path: Path, outside: Path
    ):
        destination = tmp_path / "out"
        (destination / "deep" / "er").mkdir(parents=True)
        os.symlink(outside, destination / "deep" / "er" / "and")

        result = pull(served, NAME, "-d", destination, "--force")

        assert result.code == 1
        assert "'deep/er/and'" in result.err
        assert all_names(outside) == ["precious.txt"]
        assert not (destination / "SKILL.md").exists()

    def test_a_symlinked_destination_root_is_followed_because_the_user_chose_it(
        self, served: FakeRegistry, tmp_path: Path, outside: Path
    ):
        os.symlink(outside, tmp_path / "link")

        result = pull(served, NAME, "-d", tmp_path / "link")

        assert result.code == 0
        assert (outside / "SKILL.md").is_file()
        assert (outside / "scripts" / "run.sh").is_file()
        assert (outside / "precious.txt").read_text() == "precious"

    def test_a_dangling_symlink_as_the_destination_is_reported(
        self, served: FakeRegistry, tmp_path: Path
    ):
        os.symlink(tmp_path / "nowhere", tmp_path / "link")

        result = pull(served, NAME, "-d", tmp_path / "link")

        assert result.code == 1
        assert result.err.startswith("error: ")
        assert not (tmp_path / "nowhere").exists()

    def test_special_files_and_directories_at_target_paths_are_refused(
        self, served: FakeRegistry, tmp_path: Path
    ):
        fifo_destination = tmp_path / "fifo"
        fifo_destination.mkdir()
        os.mkfifo(fifo_destination / "SKILL.md")
        directory_destination = tmp_path / "dirs"
        (directory_destination / "SKILL.md").mkdir(parents=True)
        file_destination = tmp_path / "files"
        write_tree(file_destination, {"scripts": "i should be a directory"})

        results = [
            pull(served, NAME, "-d", destination, "--force")
            for destination in (fifo_destination, directory_destination, file_destination)
        ]

        assert [result.code for result in results] == [1, 1, 1]
        assert "not a regular file" in results[0].err
        assert "not a regular file" in results[1].err
        assert "'scripts' exists and is not a directory" in results[2].err
        assert (file_destination / "scripts").read_text() == "i should be a directory"


class TestHostilePackages:
    @pytest.fixture
    def trap(self, tmp_path: Path) -> Path:
        directory = tmp_path / "trap"
        directory.mkdir()
        return directory

    def serve_and_pull(self, registry: FakeRegistry, data: bytes, destination: Path, *extra: str):
        registry.serve("evil-skill", "1.0.0", data)
        return pull(registry, "evil-skill@1.0.0", "-d", destination, *extra)

    @pytest.mark.parametrize(
        "evil_path",
        [
            "../evil.txt",
            "../../evil.txt",
            "a/../../evil.txt",
            "a/b/../../../evil.txt",
            "/evil.txt",
            "/tmp/evil.txt",
            "//evil.txt",
            "C:/evil.txt",
            "C:evil.txt",
            "..\\evil.txt",
            "a\\..\\..\\evil.txt",
            "./evil.txt",
            "a/./evil.txt",
            "a//evil.txt",
            "evil.txt/.",
            "..",
            "a/..",
        ],
    )
    def test_path_traversal_entries_are_rejected_and_nothing_is_written(
        self, registry: FakeRegistry, tmp_path: Path, evil_path: str
    ):
        destination = tmp_path / "nest" / "out"
        data = make_traversal_zip(evil_path, name="evil-skill")

        result = self.serve_and_pull(registry, data, destination)

        assert result.code == 1
        assert result.err.startswith("error: INVALID_PACKAGE (")
        assert "UNSAFE_PATH" in result.err
        assert not (tmp_path / "nest").exists()
        assert not find_anywhere(tmp_path, "evil.txt")
        assert not Path("/tmp/evil.txt").exists()

    def test_a_traversal_entry_under_a_wrapper_directory_is_rejected(
        self, registry: FakeRegistry, tmp_path: Path
    ):
        data = make_raw_package(
            [stored_entry("pkg/../../evil.txt", "x")],
            name="evil-skill",
            entrypoint_prefix="pkg/",
        )

        result = self.serve_and_pull(registry, data, tmp_path / "out")

        assert result.code == 1
        assert not find_anywhere(tmp_path, "evil.txt")

    def test_symlink_entries_are_rejected(self, registry: FakeRegistry, tmp_path: Path):
        data = make_symlink_zip("link", "/etc/passwd", name="evil-skill")

        result = self.serve_and_pull(registry, data, tmp_path / "out")

        assert result.code == 1
        assert "SYMLINK_NOT_ALLOWED" in result.err
        assert not (tmp_path / "out").exists()

    def test_a_symlink_to_a_directory_followed_by_a_file_through_it_is_rejected(
        self, registry: FakeRegistry, tmp_path: Path
    ):
        from tests.helpers.packages import UNIX_SYMLINK

        data = make_raw_package(
            [
                stored_entry("escape", str(tmp_path), external_attr=UNIX_SYMLINK),
                stored_entry("escape/evil.txt", "owned"),
            ],
            name="evil-skill",
        )

        result = self.serve_and_pull(registry, data, tmp_path / "out")

        assert result.code == 1
        assert not find_anywhere(tmp_path, "evil.txt")
        assert not (tmp_path / "out").exists()

    def test_encrypted_entries_are_rejected(self, registry: FakeRegistry, tmp_path: Path):
        result = self.serve_and_pull(
            registry, make_encrypted_zip(name="evil-skill"), tmp_path / "out"
        )

        assert result.code == 1
        assert "ENCRYPTED_ENTRY" in result.err
        assert not (tmp_path / "out").exists()

    def test_a_decompression_bomb_is_rejected_before_extraction(
        self, registry: FakeRegistry, tmp_path: Path
    ):
        data = make_bomb_zip(60 * 1024 * 1024, declared_size=10, name="evil-skill")
        assert len(data) < 200_000

        result = self.serve_and_pull(registry, data, tmp_path / "out")

        assert result.code == 1
        assert "TOO_LARGE" in result.err
        assert not (tmp_path / "out").exists()

    def test_paths_that_collide_after_case_folding_are_rejected(
        self, registry: FakeRegistry, tmp_path: Path
    ):
        data = make_raw_package(
            [stored_entry("Data.txt", "one"), stored_entry("data.TXT", "two")], name="evil-skill"
        )

        result = self.serve_and_pull(registry, data, tmp_path / "out")

        assert result.code == 1
        assert "DUPLICATE_PATH" in result.err
        assert not (tmp_path / "out").exists()

    def test_a_file_that_is_also_a_directory_is_rejected(
        self, registry: FakeRegistry, tmp_path: Path
    ):
        data = make_raw_package(
            [stored_entry("thing", "file"), stored_entry("thing/inner.txt", "nested")],
            name="evil-skill",
        )

        result = self.serve_and_pull(registry, data, tmp_path / "out")

        assert result.code == 1
        assert "DUPLICATE_PATH" in result.err

    def test_content_that_is_not_a_zip_is_rejected(self, registry: FakeRegistry, tmp_path: Path):
        result = self.serve_and_pull(registry, b"<html>captive portal</html>", tmp_path / "out")

        assert result.code == 1
        assert "NOT_A_ZIP" in result.err
        assert not (tmp_path / "out").exists()

    def test_force_does_not_relax_the_validation(self, registry: FakeRegistry, tmp_path: Path):
        data = make_traversal_zip("../evil.txt", name="evil-skill")

        result = self.serve_and_pull(registry, data, tmp_path / "out", "--force")

        assert result.code == 1
        assert not find_anywhere(tmp_path, "evil.txt")

    def test_a_valid_package_in_a_hostile_registry_still_extracts_only_inside(
        self, registry: FakeRegistry, tmp_path: Path
    ):
        data = make_skill_zip("evil-skill", "1.0.0", files={"..hidden": "x", "a..b/c": "y"})

        result = self.serve_and_pull(registry, data, tmp_path / "out")

        assert result.code == 0
        assert all_names(tmp_path / "out") == ["..hidden", "SKILL.md", "a..b/c"]
        assert sorted(item.name for item in tmp_path.iterdir()) == ["out"]


class TestServerTrust:
    def test_a_wrong_hash_extracts_nothing(self, served: FakeRegistry, tmp_path: Path):
        data = served.entries[NAME].versions["1.0.0"].data
        served.serve(NAME, "1.0.0", data, sha256="f" * 64)

        result = pull(served, f"{NAME}@1.0.0", "-d", tmp_path / "out")

        assert result.code == 1
        assert "sha256 mismatch" in result.err
        assert not (tmp_path / "out").exists()

    def test_a_missing_hash_header_extracts_nothing(self, served: FakeRegistry, tmp_path: Path):
        data = served.entries[NAME].versions["1.0.0"].data
        served.serve(NAME, "1.0.0", data, sha256=None)

        result = pull(served, f"{NAME}@1.0.0", "-d", tmp_path / "out")

        assert result.code == 1
        assert "X-Content-SHA256" in result.err
        assert not (tmp_path / "out").exists()

    def test_a_package_with_another_name_is_refused(self, registry: FakeRegistry, tmp_path: Path):
        registry.serve("wanted", "1.0.0", make_skill_zip("something-else", "1.0.0"))

        result = pull(registry, "wanted@1.0.0", "-d", tmp_path / "out")

        assert result.code == 1
        assert result.err.startswith("error: INTEGRITY_ERROR: the package declares the name")
        assert "'something-else'" in result.err and "'wanted'" in result.err
        assert not (tmp_path / "out").exists()

    def test_a_package_with_another_version_is_refused(
        self, registry: FakeRegistry, tmp_path: Path
    ):
        registry.serve("wanted", "1.0.0", make_skill_zip("wanted", "2.0.0"))

        result = pull(registry, "wanted@1.0.0", "-d", tmp_path / "out")

        assert result.code == 1
        assert result.err.startswith("error: INTEGRITY_ERROR: the package declares the version")
        assert not (tmp_path / "out").exists()

    def test_latest_accepts_whatever_version_the_server_resolves(
        self, registry: FakeRegistry, tmp_path: Path
    ):
        registry.serve("wanted", "latest", make_skill_zip("wanted", "7.1.0"))

        result = pull(registry, "wanted", "-d", tmp_path / "out")

        assert result.code == 0
        assert "7.1.0" in result.out.splitlines()[0]

    def test_a_uuid_reference_does_not_require_the_name_to_match(
        self, registry: FakeRegistry, tmp_path: Path
    ):
        reference = str(uuid.uuid4())
        registry.serve(reference, "latest", make_skill_zip("real-name", "1.0.0"))

        result = pull(registry, reference, "-d", tmp_path / "out")

        assert result.code == 0
        assert "real-name" in result.out

    def test_an_unknown_package_is_reported(self, served: FakeRegistry, tmp_path: Path):
        result = pull(served, "missing-package", "-d", tmp_path / "out")

        assert result.code == 1
        assert result.err.startswith("error: NOT_FOUND: ")
        assert not (tmp_path / "out").exists()

    def test_json_mode_reports_failures_as_json(self, served: FakeRegistry, tmp_path: Path):
        result = pull(served, "missing-package", "-d", tmp_path / "out", "--json")

        assert result.code == 1
        assert result.out == ""
        assert result.error_json()["error"]["code"] == "NOT_FOUND"

    def test_json_mode_reports_package_failures_with_their_reason(
        self, registry: FakeRegistry, tmp_path: Path
    ):
        registry.serve("evil-skill", "1.0.0", make_traversal_zip(name="evil-skill"))

        result = pull(registry, "evil-skill@1.0.0", "-d", tmp_path / "out", "--json")

        error = result.error_json()["error"]
        assert (error["code"], error["reason"]) == ("INVALID_PACKAGE", "UNSAFE_PATH")

    def test_invalid_references_are_usage_errors(self, served: FakeRegistry):
        assert pull(served, "Bad Name").code == 2
        assert pull(served, "name@1.0").code == 2
        assert served.requests == []


class TestTransactionalBehaviour:
    def test_a_failure_while_staging_leaves_the_destination_as_it_was(
        self, served: FakeRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        destination = write_tree(tmp_path / "out", {"mine.txt": "mine"})
        before = tree_snapshot(destination)
        original = cli._write_member
        calls = []

        def failing(archive, item, temporary):
            calls.append(item.entry.path)
            if len(calls) == 4:
                raise cli.CliError("simulated failure", code="INTEGRITY_ERROR")
            return original(archive, item, temporary)

        monkeypatch.setattr(cli, "_write_member", failing)

        result = pull(served, NAME, "-d", destination)

        assert result.code == 1
        assert "simulated failure" in result.err
        assert len(calls) == 4
        assert tree_snapshot(destination) == before
        assert sorted(item.name for item in destination.iterdir()) == ["mine.txt"]

    def test_a_failure_while_staging_removes_a_destination_it_created(
        self, served: FakeRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        original = cli._write_member
        calls = []

        def failing(archive, item, temporary):
            calls.append(1)
            if len(calls) == 5:
                raise OSError(errno.ENOSPC, "No space left on device")
            return original(archive, item, temporary)

        monkeypatch.setattr(cli, "_write_member", failing)
        destination = tmp_path / "fresh" / "nested"

        result = pull(served, NAME, "-d", destination)

        assert result.code == 1
        assert "No space left on device" in result.err
        assert "Traceback" not in result.err
        assert not (tmp_path / "fresh" / "nested").exists()
        assert not (tmp_path / "fresh").exists()

    def test_a_failure_while_committing_removes_what_was_placed(
        self, served: FakeRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        destination = tmp_path / "out"
        original = os.replace
        placed = []

        def flaky(source, target, *args, **kwargs):
            if str(target).startswith(str(destination)):
                placed.append(target)
                if len(placed) == 3:
                    raise OSError(errno.EIO, "Input/output error")
            return original(source, target, *args, **kwargs)

        monkeypatch.setattr(os, "replace", flaky)

        result = pull(served, NAME, "-d", destination)

        assert result.code == 1
        assert "Input/output error" in result.err
        assert not destination.exists()

    def test_a_failure_while_committing_keeps_pre_existing_directories(
        self, served: FakeRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        destination = write_tree(tmp_path / "out", {"mine.txt": "mine"})
        before = tree_snapshot(destination)
        original = os.replace
        placed = []

        def flaky(source, target, *args, **kwargs):
            if str(target).startswith(str(destination)):
                placed.append(target)
                if len(placed) == 5:
                    raise OSError(errno.EIO, "Input/output error")
            return original(source, target, *args, **kwargs)

        monkeypatch.setattr(os, "replace", flaky)

        result = pull(served, NAME, "-d", destination)

        assert result.code == 1
        assert tree_snapshot(destination) == before
        assert sorted(item.name for item in destination.iterdir()) == ["mine.txt"]

    def test_an_unwritable_destination_is_reported_without_residue(
        self, served: FakeRegistry, tmp_path: Path
    ):
        if os.geteuid() == 0:
            pytest.skip("root ignores permissions")
        destination = tmp_path / "readonly"
        destination.mkdir()
        destination.chmod(0o555)
        try:
            result = pull(served, NAME, "-d", destination)
        finally:
            destination.chmod(0o755)

        assert result.code == 1
        assert "Permission denied" in result.err
        assert list(destination.iterdir()) == []

    def test_interruption_removes_the_staged_files(
        self, served: FakeRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        original = cli._write_member
        calls = []

        def interrupted(archive, item, temporary):
            calls.append(1)
            if len(calls) == 3:
                raise KeyboardInterrupt
            return original(archive, item, temporary)

        monkeypatch.setattr(cli, "_write_member", interrupted)
        destination = tmp_path / "out"

        result = pull(served, NAME, "-d", destination)

        assert result.code == 130
        assert result.err == "interrupted\n"
        assert not destination.exists()
