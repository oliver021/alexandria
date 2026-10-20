from __future__ import annotations

import errno
import hashlib
import io
import os
import stat
import unicodedata
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from app import cli
from app.cli import CliError, extract_package, pack_directory
from app.services.errors import InvalidPackageError
from app.services.package_inspector import ManifestEntry, ParsedPackage, inspect_package
from tests.helpers.packages import make_skill_zip
from tests.test_cli.support import skill_tree, tree_snapshot

pytestmark = pytest.mark.skipif(os.name == "nt", reason="relies on POSIX file semantics")

ZIP_DATE = (2020, 1, 1, 0, 0, 0)


def zip_of(members: dict[str, bytes | tuple[bytes, int, int]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in members.items():
            content, attributes, system = (
                value if isinstance(value, tuple) else (value, 0o100644 << 16, 3)
            )
            info = zipfile.ZipInfo(name, ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = attributes
            info.create_system = system
            archive.writestr(info, content)
    return buffer.getvalue()


def zip_with_mode(attributes: int) -> bytes:
    return zip_of({"tool.sh": (b"#!/bin/sh\n", attributes << 16, 3)})


def entry_for(path: str, content: bytes) -> ManifestEntry:
    return ManifestEntry(
        path=path,
        size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        media_type="text/plain",
        text=True,
    )


@pytest.fixture
def parsed_base() -> ParsedPackage:
    return inspect_package(make_skill_zip("base-skill", "1.0.0"))


def parsed_with(parsed_base: ParsedPackage, manifest: list[ManifestEntry], prefix: str = ""):
    return replace(parsed_base, manifest=manifest, root_prefix=prefix)


def nothing_created(tmp_path: Path) -> bool:
    return not any(tmp_path.iterdir())


class TestHostileManifests:
    @pytest.mark.parametrize(
        "path",
        [
            "../evil",
            "../../evil",
            "a/../../evil",
            "a/b/../../../evil",
            "/evil",
            "/etc/passwd",
            "//evil",
            "a//b",
            "./a",
            "a/./b",
            ".",
            "..",
            "a/..",
            "a/.",
            "a/",
            "",
            "a\\b",
            "..\\evil",
            "C:evil",
            "C:/evil",
            "a\x00b",
            "a\nb",
            "a\x1bb",
            "a\u202eb",
            "x" * 300,
        ],
    )
    def test_unsafe_manifest_paths_are_rejected_before_anything_is_written(
        self, tmp_path: Path, parsed_base: ParsedPackage, path: str
    ):
        data = zip_of({"ok.txt": b"ok"})
        parsed = parsed_with(parsed_base, [entry_for(path, b"ok")])
        destination = tmp_path / "dest"

        with pytest.raises(InvalidPackageError) as raised:
            extract_package(data, parsed, destination)

        assert raised.value.reason == "UNSAFE_PATH"
        assert not destination.exists()
        assert nothing_created(tmp_path)

    def test_one_unsafe_entry_among_good_ones_blocks_the_whole_extraction(
        self, tmp_path: Path, parsed_base: ParsedPackage
    ):
        data = zip_of({"good.txt": b"g", "other.txt": b"o"})
        manifest = [entry_for("good.txt", b"g"), entry_for("../evil", b"o")]
        destination = tmp_path / "dest"

        with pytest.raises(InvalidPackageError):
            extract_package(data, parsed_with(parsed_base, manifest), destination)

        assert nothing_created(tmp_path)

    def test_paths_that_collide_after_folding_are_rejected(
        self, tmp_path: Path, parsed_base: ParsedPackage
    ):
        data = zip_of({"a.txt": b"1", "A.TXT": b"2"})
        manifest = [entry_for("a.txt", b"1"), entry_for("A.TXT", b"2")]

        with pytest.raises(InvalidPackageError) as raised:
            extract_package(data, parsed_with(parsed_base, manifest), tmp_path / "dest")

        assert raised.value.reason == "DUPLICATE_PATH"
        assert nothing_created(tmp_path)

    def test_canonically_equivalent_names_are_treated_as_duplicates(
        self, tmp_path: Path, parsed_base: ParsedPackage
    ):
        composed = unicodedata.normalize("NFC", "caf\u00e9.txt")
        decomposed = unicodedata.normalize("NFD", "caf\u00e9.txt")
        assert composed != decomposed
        data = zip_of({composed: b"1", decomposed: b"2"})
        manifest = [entry_for(composed, b"1"), entry_for(decomposed, b"2")]

        with pytest.raises(InvalidPackageError) as raised:
            extract_package(data, parsed_with(parsed_base, manifest), tmp_path / "dest")

        assert raised.value.reason == "DUPLICATE_PATH"

    def test_a_path_that_is_both_file_and_directory_is_rejected(
        self, tmp_path: Path, parsed_base: ParsedPackage
    ):
        data = zip_of({"thing": b"f", "thing/inner": b"i"})
        manifest = [entry_for("thing", b"f"), entry_for("thing/inner", b"i")]

        with pytest.raises(InvalidPackageError) as raised:
            extract_package(data, parsed_with(parsed_base, manifest), tmp_path / "dest")

        assert raised.value.reason == "DUPLICATE_PATH"
        assert "both as a file and as a directory" in raised.value.message
        assert nothing_created(tmp_path)

    def test_a_symlink_member_is_rejected_even_if_the_manifest_lists_it(
        self, tmp_path: Path, parsed_base: ParsedPackage
    ):
        link = (b"/etc/passwd", 0o120777 << 16, 3)
        data = zip_of({"ok.txt": b"ok", "link": link})
        manifest = [entry_for("link", b"/etc/passwd"), entry_for("ok.txt", b"ok")]

        with pytest.raises(InvalidPackageError) as raised:
            extract_package(data, parsed_with(parsed_base, manifest), tmp_path / "dest")

        assert raised.value.reason == "SYMLINK_NOT_ALLOWED"
        assert nothing_created(tmp_path)

    def test_a_symlink_flag_is_honoured_whatever_the_creating_system(
        self, tmp_path: Path, parsed_base: ParsedPackage
    ):
        data = zip_of({"link": (b"x", 0o120777 << 16, 0)})

        with pytest.raises(InvalidPackageError) as raised:
            extract_package(
                data, parsed_with(parsed_base, [entry_for("link", b"x")]), tmp_path / "dest"
            )

        assert raised.value.reason == "SYMLINK_NOT_ALLOWED"


class TestIntegrityOfMembers:
    def test_a_manifest_entry_missing_from_the_archive_is_an_integrity_error(
        self, tmp_path: Path, parsed_base: ParsedPackage
    ):
        data = zip_of({"present.txt": b"p"})
        manifest = [entry_for("present.txt", b"p"), entry_for("ghost.txt", b"g")]

        with pytest.raises(CliError) as raised:
            extract_package(data, parsed_with(parsed_base, manifest), tmp_path / "dest")

        assert raised.value.code == "INTEGRITY_ERROR"
        assert "'ghost.txt'" in raised.value.message
        assert nothing_created(tmp_path)

    def test_the_root_prefix_locates_the_members(self, tmp_path: Path, parsed_base: ParsedPackage):
        data = zip_of({"pkg/inner/a.txt": b"A", "pkg/b.txt": b"B"})
        manifest = [entry_for("inner/a.txt", b"A"), entry_for("b.txt", b"B")]
        destination = tmp_path / "dest"

        files = extract_package(data, parsed_with(parsed_base, manifest, "pkg/"), destination)

        assert files == ["b.txt", "inner/a.txt"]
        assert (destination / "inner" / "a.txt").read_bytes() == b"A"

    def test_a_wrong_root_prefix_finds_nothing(self, tmp_path: Path, parsed_base: ParsedPackage):
        data = zip_of({"pkg/a.txt": b"A"})

        with pytest.raises(CliError) as raised:
            extract_package(
                data, parsed_with(parsed_base, [entry_for("a.txt", b"A")], ""), tmp_path / "d"
            )

        assert raised.value.code == "INTEGRITY_ERROR"

    @pytest.mark.parametrize(
        ("declared", "actual"),
        [
            (b"short", b"longer content"),
            (b"longer content", b"short"),
            (b"same-size-1", b"same-size-2"),
        ],
    )
    def test_content_that_differs_from_the_manifest_is_rejected_and_rolled_back(
        self, tmp_path: Path, parsed_base: ParsedPackage, declared: bytes, actual: bytes
    ):
        data = zip_of({"a.txt": b"fine", "b.txt": actual})
        manifest = [entry_for("a.txt", b"fine"), entry_for("b.txt", declared)]
        destination = tmp_path / "dest"

        with pytest.raises(CliError) as raised:
            extract_package(data, parsed_with(parsed_base, manifest), destination)

        assert raised.value.code == "INTEGRITY_ERROR"
        assert "does not match its manifest entry" in raised.value.message
        assert not destination.exists()
        assert nothing_created(tmp_path)

    def test_a_member_with_a_corrupt_stream_is_reported_and_rolled_back(
        self, tmp_path: Path, parsed_base: ParsedPackage
    ):
        data = bytearray(zip_of({"a.txt": b"x" * 500}))
        offset = data.index(b"a.txt") + len(b"a.txt") + 3
        data[offset] ^= 0xFF
        manifest = [entry_for("a.txt", b"x" * 500)]

        with pytest.raises(CliError) as raised:
            extract_package(bytes(data), parsed_with(parsed_base, manifest), tmp_path / "dest")

        assert raised.value.code == "INTEGRITY_ERROR"
        assert "cannot read 'a.txt' from the package" in raised.value.message
        assert nothing_created(tmp_path)

    def test_an_archive_that_is_not_a_zip_is_corrupt(
        self, tmp_path: Path, parsed_base: ParsedPackage
    ):
        with pytest.raises(InvalidPackageError) as raised:
            extract_package(b"not a zip at all", parsed_base, tmp_path / "dest")

        assert raised.value.reason == "CORRUPT_ARCHIVE"
        assert nothing_created(tmp_path)


class TestModes:
    @pytest.mark.parametrize(
        ("attributes", "system", "expected"),
        [
            (0o100755 << 16, 3, 0o755),
            (0o100644 << 16, 3, 0o644),
            (0o100700 << 16, 3, 0o755),
            (0o104755 << 16, 3, 0o755),
            (0o100777 << 16, 3, 0o755),
            (0o100666 << 16, 3, 0o644),
            (0o100010 << 16, 3, 0o644),
            (0, 3, 0o644),
            (0o100755 << 16, 0, 0o644),
            (0x20, 0, 0o644),
        ],
    )
    def test_modes_are_normalised(
        self,
        tmp_path: Path,
        parsed_base: ParsedPackage,
        attributes: int,
        system: int,
        expected: int,
    ):
        data = zip_of({"f": (b"x", attributes, system)})
        destination = tmp_path / "dest"

        extract_package(data, parsed_with(parsed_base, [entry_for("f", b"x")]), destination)

        assert stat.S_IMODE((destination / "f").stat().st_mode) == expected

    def test_setuid_and_sticky_bits_never_survive(self, tmp_path: Path, parsed_base: ParsedPackage):
        data = zip_of({"f": (b"x", 0o107777 << 16, 3)})
        destination = tmp_path / "dest"

        extract_package(data, parsed_with(parsed_base, [entry_for("f", b"x")]), destination)

        assert stat.S_IMODE((destination / "f").stat().st_mode) == 0o755


class TestFilesystemSafety:
    def build(self, parsed_base: ParsedPackage, members: dict[str, bytes]):
        data = zip_of(members)
        manifest = [entry_for(path, content) for path, content in members.items()]
        return data, parsed_with(parsed_base, manifest)

    def test_the_returned_paths_are_sorted(self, tmp_path: Path, parsed_base: ParsedPackage):
        data, parsed = self.build(parsed_base, {"z.txt": b"z", "a/b.txt": b"b", "a.txt": b"a"})

        files = extract_package(data, parsed, tmp_path / "dest")

        assert files == ["a.txt", "a/b.txt", "z.txt"]

    def test_modes_are_applied_by_path_where_descriptors_cannot_be_used(
        self, tmp_path: Path, parsed_base: ParsedPackage, monkeypatch: pytest.MonkeyPatch
    ):
        parsed = parsed_with(parsed_base, [entry_for("tool.sh", b"#!/bin/sh\n")])
        monkeypatch.delattr(os, "fchmod")
        destination = tmp_path / "dest"

        extract_package(zip_with_mode(0o100755), parsed, destination)

        assert stat.S_IMODE((destination / "tool.sh").stat().st_mode) == 0o755

    def test_an_empty_file_is_extracted(self, tmp_path: Path, parsed_base: ParsedPackage):
        data, parsed = self.build(parsed_base, {"empty.txt": b""})

        extract_package(data, parsed, tmp_path / "dest")

        assert (tmp_path / "dest" / "empty.txt").read_bytes() == b""

    def test_a_file_appearing_between_the_check_and_the_commit_is_not_clobbered(
        self, tmp_path: Path, parsed_base: ParsedPackage, monkeypatch: pytest.MonkeyPatch
    ):
        data, parsed = self.build(parsed_base, {"a.txt": b"a", "b.txt": b"b"})
        destination = tmp_path / "dest"
        original = cli._write_member

        def racing(archive, item, temporary):
            original(archive, item, temporary)
            if item.entry.path == "b.txt":
                (destination / "a.txt").write_text("planted by someone else")

        monkeypatch.setattr(cli, "_write_member", racing)

        with pytest.raises(CliError) as raised:
            extract_package(data, parsed, destination)

        assert raised.value.code == "CONFLICT"
        assert "appeared while extracting" in raised.value.message
        assert tree_snapshot(destination).keys() == {"a.txt"}
        assert (destination / "a.txt").read_text() == "planted by someone else"

    def test_force_accepts_a_file_that_appeared_meanwhile(
        self, tmp_path: Path, parsed_base: ParsedPackage, monkeypatch: pytest.MonkeyPatch
    ):
        data, parsed = self.build(parsed_base, {"a.txt": b"a"})
        destination = tmp_path / "dest"
        original = cli._write_member

        def racing(archive, item, temporary):
            original(archive, item, temporary)
            (destination / "a.txt").write_text("planted")

        monkeypatch.setattr(cli, "_write_member", racing)

        extract_package(data, parsed, destination, force=True)

        assert (destination / "a.txt").read_bytes() == b"a"

    @pytest.mark.parametrize("kind", ["symlink", "directory"])
    def test_a_target_swapped_for_a_link_or_directory_meanwhile_is_refused(
        self,
        tmp_path: Path,
        parsed_base: ParsedPackage,
        monkeypatch: pytest.MonkeyPatch,
        kind: str,
    ):
        data, parsed = self.build(parsed_base, {"a.txt": b"a"})
        destination = tmp_path / "dest"
        secret = tmp_path / "secret.txt"
        secret.write_text("secret")
        original = cli._write_member

        def swapping(archive, item, temporary):
            original(archive, item, temporary)
            if kind == "symlink":
                os.symlink(secret, destination / "a.txt")
            else:
                (destination / "a.txt").mkdir()

        monkeypatch.setattr(cli, "_write_member", swapping)

        with pytest.raises(CliError) as raised:
            extract_package(data, parsed, destination, force=True)

        assert "is not a regular file" in raised.value.message
        assert secret.read_text() == "secret"

    def test_ensure_directory_refuses_links_and_files(self, tmp_path: Path):
        outside = tmp_path / "outside"
        outside.mkdir()
        root = tmp_path / "root"
        root.mkdir()
        os.symlink(outside, root / "link")
        (root / "file").write_text("x")
        workspace = cli._Workspace()

        with pytest.raises(CliError, match="symbolic link 'link'"):
            cli._ensure_directory(root, ["link", "deeper"], workspace)
        with pytest.raises(CliError, match="'file' exists and is not a directory"):
            cli._ensure_directory(root, ["file", "deeper"], workspace)

        assert list(outside.iterdir()) == []
        assert workspace.directories == []

    def test_ensure_directory_records_what_it_creates(self, tmp_path: Path):
        workspace = cli._Workspace()

        folder = cli._ensure_directory(tmp_path, ["a", "b", "c"], workspace)
        again = cli._ensure_directory(tmp_path, ["a", "b", "c"], workspace)

        assert folder == again == tmp_path / "a" / "b" / "c"
        assert workspace.directories == [tmp_path / "a", tmp_path / "a" / "b", folder]

    def test_assert_inside_accepts_only_strict_descendants(self, tmp_path: Path):
        cli._assert_inside(tmp_path, tmp_path / "a" / "b")
        for outside in (
            tmp_path,
            tmp_path.parent,
            tmp_path.parent / "sibling",
            Path("/etc/passwd"),
        ):
            with pytest.raises(InvalidPackageError) as raised:
                cli._assert_inside(tmp_path, outside)
            assert raised.value.reason == "UNSAFE_PATH"

    def test_assert_inside_is_not_fooled_by_a_shared_name_prefix(self, tmp_path: Path):
        root = tmp_path / "dest"
        sibling = tmp_path / "dest-evil" / "x"

        with pytest.raises(InvalidPackageError):
            cli._assert_inside(root, sibling)

    def test_locate_root_reports_the_chain_of_missing_directories(self, tmp_path: Path):
        existing = tmp_path / "exists"
        existing.mkdir()

        root, missing = cli._locate_root(existing / "a" / "b")
        assert root == existing / "a" / "b"
        assert missing == [existing / "a", existing / "a" / "b"]

        root, missing = cli._locate_root(existing)
        assert (root, missing) == (existing.resolve(), [])

    def test_locate_root_refuses_a_file(self, tmp_path: Path):
        (tmp_path / "file").write_text("x")

        with pytest.raises(CliError, match="exists and is not a directory"):
            cli._locate_root(tmp_path / "file")

    def test_the_workspace_never_removes_non_empty_directories(self, tmp_path: Path):
        workspace = cli._Workspace()
        folder = cli._ensure_directory(tmp_path, ["a"], workspace)
        (folder / "user-data.txt").write_text("keep")

        workspace.rollback()

        assert (folder / "user-data.txt").read_text() == "keep"

    def test_errors_from_the_filesystem_become_clean_errors(
        self, tmp_path: Path, parsed_base: ParsedPackage, monkeypatch: pytest.MonkeyPatch
    ):
        data, parsed = self.build(parsed_base, {"a.txt": b"a"})

        def broken(path, mode):
            raise OSError(errno.ENAMETOOLONG, "File name too long")

        monkeypatch.setattr(os, "mkdir", broken)

        with pytest.raises(CliError, match="File name too long"):
            extract_package(data, parsed, tmp_path / "dest")


class TestWindowsNames:
    @pytest.mark.parametrize(
        "segment",
        ["con", "CON", "con.txt", "Nul.tar.gz", "prn", "aux", "com1", "COM9.log", "lpt1", "lpt9.x"],
    )
    def test_reserved_device_names(self, segment: str):
        assert cli._windows_problem(segment) == "is a reserved device name on Windows"

    @pytest.mark.parametrize("segment", ["name.", "name ", "dots..", "a "])
    def test_trailing_dots_and_spaces(self, segment: str):
        assert cli._windows_problem(segment) == "ends with a dot or a space"

    def test_colons_select_alternate_streams(self):
        assert cli._windows_problem("file:stream") == "contains a colon"

    @pytest.mark.parametrize(
        "segment", ["console", "com10", "lpt0", "a.b", "nulled", "SKILL.md", "x y", ".hidden"]
    )
    def test_ordinary_names_are_fine(self, segment: str):
        assert cli._windows_problem(segment) is None

    def test_the_rules_apply_only_when_running_on_windows(self, monkeypatch: pytest.MonkeyPatch):
        assert cli._checked_parts("dir/con.txt") == ("dir", "con.txt")

        monkeypatch.setattr(cli, "_IS_WINDOWS", True)

        with pytest.raises(InvalidPackageError) as raised:
            cli._checked_parts("dir/con.txt")
        assert raised.value.reason == "UNSAFE_PATH"
        assert cli._checked_parts("dir/ok.txt") == ("dir", "ok.txt")


class TestRealPackages:
    def test_extracting_a_real_package_matches_its_manifest(self, tmp_path: Path):
        source = skill_tree(tmp_path / "src", files={"x/y/z.bin": b"\x00\x01\x02"})
        data = pack_directory(source).data
        parsed = inspect_package(data)

        extract_package(data, parsed, tmp_path / "dest")

        snapshot = tree_snapshot(tmp_path / "dest")
        digests = {
            path: hashlib.sha256(content).hexdigest() for path, (content, _) in snapshot.items()
        }
        assert digests == {entry.path: entry.sha256 for entry in parsed.manifest}
