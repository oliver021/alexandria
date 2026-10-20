import copy
import hashlib
import io
import itertools
import json
import os
import pickle
import random
import shutil
import struct
import subprocess
import sys
import threading
import time
import tracemalloc
import unicodedata
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.services import package_inspector as inspector
from app.services.errors import (
    PACKAGE_REASONS,
    ConflictError,
    DomainError,
    ForbiddenError,
    InvalidPackageError,
    NotFoundError,
    PayloadTooLargeError,
    StorageError,
    UnauthorizedError,
    ValidationError,
)
from app.services.package_inspector import (
    DEFAULT_LIMITS,
    ManifestEntry,
    PackageLimits,
    ParsedPackage,
    guess_media_type,
    inspect_package,
    is_text_sample,
    looks_like_uuid,
    read_package_file,
    split_frontmatter,
    validate_requested_path,
)
from app.services.semver import InvalidVersionError
from tests.helpers.packages import (
    APPLE_DOUBLE_BODY,
    DESCRIPTOR_FLAG,
    DS_STORE_BODY,
    UNIX_DIRECTORY,
    UNIX_SYMLINK,
    ZIP64_MARKER,
    LayoutEntry,
    build_layout_zip,
    build_raw_zip,
    central_directory_offset,
    central_records,
    corrupt_entry_data,
    data_descriptor,
    deflate_bytes,
    deflate_carrier,
    deflate_streaming_walk,
    deflated_entry,
    end_fields,
    end_record_offset,
    entry_extent,
    forge_compressed_size,
    forge_crc,
    forge_declared_size,
    frontmatter_doc,
    has_zip64_end,
    index_md,
    last_central_record,
    layout_bomb,
    layout_deflated,
    layout_directory,
    layout_local_record,
    layout_stored,
    length_honouring_names,
    local_header_offset,
    make_bomb_zip,
    make_directory_flood_zip,
    make_encrypted_zip,
    make_grounding_zip,
    make_macos_zip,
    make_raw_package,
    make_skill_zip,
    make_stream_zip,
    make_symlink_zip,
    make_traversal_zip,
    make_zeros_zip,
    make_zip,
    make_zip64_zip,
    make_zipalign_zip,
    overflow_central_directory,
    set_central_fields,
    set_compression_method,
    set_end_fields,
    set_external_attr,
    set_flag_bits,
    set_local_fields,
    set_local_header_offset,
    skill_md,
    splice_bytes,
    stored_entry,
    streaming_names,
    unicode_path_extra,
    with_zip64_end,
    zero_bomb_entry,
    zip64_extra,
)

KIB = 1024
MIB = 1024 * KIB
GIB = 1024 * MIB

VALID_BLOCK = "name: sample\ndescription: Sample description\nversion: 1.0.0\n"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
EURO = chr(0x20AC)
E_ACUTE = chr(0xE9)
COMBINING_ACUTE = chr(0x301)


@pytest.fixture(autouse=True)
def setup_db():
    yield


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def paths_of(parsed: ParsedPackage) -> list[str]:
    return [entry.path for entry in parsed.manifest]


def manifest_of(parsed: ParsedPackage) -> dict[str, ManifestEntry]:
    return {entry.path: entry for entry in parsed.manifest}


def inspect_text(text, **kwargs) -> ParsedPackage:
    return inspect_package(make_zip({"SKILL.md": text}), **kwargs)


def inspect_block(block, body="", *, version="1.0.0", **kwargs) -> ParsedPackage:
    return inspect_text(frontmatter_doc(block, body), version=version, **kwargs)


def rejection(data, limits=DEFAULT_LIMITS, **kwargs) -> InvalidPackageError:
    with pytest.raises(InvalidPackageError) as caught:
        inspect_package(data, limits, **kwargs)
    return caught.value


def reason_of(data, limits=DEFAULT_LIMITS, **kwargs) -> str:
    return rejection(data, limits, **kwargs).reason


def block_rejection(block, body="", *, version="1.0.0") -> InvalidPackageError:
    return rejection(make_zip({"SKILL.md": frontmatter_doc(block, body)}), version=version)


class CountingHashlib:
    def __init__(self):
        self.hashed = 0

    def sha256(self, data=b""):
        counter = self
        digest = hashlib.sha256(data)
        counter.hashed += len(data)

        class Digest:
            def update(self, chunk):
                counter.hashed += len(chunk)
                digest.update(chunk)

            def hexdigest(self):
                return digest.hexdigest()

        return Digest()


class CountingZlib:
    def __init__(self):
        self.inflated = 0

    def decompressobj(self, *arguments):
        counter = self
        inner = zlib.decompressobj(*arguments)

        class Inflater:
            def decompress(self, data, max_length=0):
                chunk = inner.decompress(data, max_length)
                counter.inflated += len(chunk)
                return chunk

            def __getattr__(self, name):
                return getattr(inner, name)

        return Inflater()

    def __getattr__(self, name):
        return getattr(zlib, name)


class TestDomainErrors:
    @pytest.mark.parametrize(
        ("error_class", "code"),
        [
            (NotFoundError, "NOT_FOUND"),
            (ConflictError, "CONFLICT"),
            (ValidationError, "VALIDATION_ERROR"),
            (ForbiddenError, "FORBIDDEN"),
            (UnauthorizedError, "UNAUTHORIZED"),
            (PayloadTooLargeError, "PAYLOAD_TOO_LARGE"),
        ],
    )
    def test_each_error_carries_its_contract_code_and_message(self, error_class, code):
        error = error_class("something happened")
        assert error.code == code
        assert error.message == "something happened"
        assert str(error) == "something happened"
        assert error.reason is None
        assert isinstance(error, DomainError)

    def test_invalid_package_error_carries_a_reason(self):
        error = InvalidPackageError("TOO_LARGE", "too big")
        assert error.code == "INVALID_PACKAGE"
        assert error.reason == "TOO_LARGE"
        assert error.message == "too big"
        assert str(error) == "too big"
        assert isinstance(error, DomainError)

    def test_invalid_package_error_has_a_default_message_naming_the_reason(self):
        error = InvalidPackageError("UNSAFE_PATH")
        assert "UNSAFE_PATH" in error.message

    def test_validation_error_may_carry_a_reason(self):
        error = ValidationError("bad path", reason="INVALID_PATH")
        assert error.code == "VALIDATION_ERROR"
        assert error.reason == "INVALID_PATH"

    @pytest.mark.parametrize(
        "error_class",
        [
            NotFoundError,
            ConflictError,
            ValidationError,
            ForbiddenError,
            UnauthorizedError,
            PayloadTooLargeError,
        ],
    )
    def test_errors_have_a_default_message(self, error_class):
        assert error_class().message
        assert str(error_class())

    @pytest.mark.parametrize(
        "error_class",
        [
            NotFoundError,
            ConflictError,
            ValidationError,
            InvalidPackageError,
            ForbiddenError,
            UnauthorizedError,
            PayloadTooLargeError,
        ],
    )
    def test_every_domain_error_is_a_value_error(self, error_class):
        assert issubclass(error_class, DomainError)
        assert issubclass(error_class, ValueError)
        with pytest.raises(ValueError):
            raise error_class("x") if error_class is not InvalidPackageError else error_class("X")

    def test_domain_errors_survive_pickle_and_deepcopy(self):
        originals = [
            InvalidPackageError("TOO_LARGE", "too big"),
            ValidationError("bad path", reason="INVALID_PATH"),
            NotFoundError("missing"),
        ]
        for original in originals:
            for clone in (pickle.loads(pickle.dumps(original)), copy.deepcopy(original)):
                assert type(clone) is type(original)
                assert clone.code == original.code
                assert clone.message == original.message
                assert clone.reason == original.reason
                assert str(clone) == str(original)

    def test_contract_reason_catalog(self):
        assert len(PACKAGE_REASONS) == 16
        assert len(set(PACKAGE_REASONS)) == 16
        assert set(PACKAGE_REASONS) == {
            "NOT_A_ZIP",
            "CORRUPT_ARCHIVE",
            "ENCRYPTED_ENTRY",
            "UNSUPPORTED_COMPRESSION",
            "SYMLINK_NOT_ALLOWED",
            "UNSAFE_PATH",
            "DUPLICATE_PATH",
            "TOO_MANY_FILES",
            "TOO_LARGE",
            "MISSING_ENTRYPOINT",
            "AMBIGUOUS_ENTRYPOINT",
            "BAD_FRONTMATTER",
            "MISSING_FIELD",
            "INVALID_FIELD",
            "KIND_MISMATCH",
            "VERSION_MISMATCH",
        }


class TestLimitsObject:
    def test_defaults_match_the_contract(self):
        limits = PackageLimits()
        assert limits.max_package_bytes == 25 * MIB == 26214400
        assert limits.max_uncompressed_bytes == 100 * MIB == 104857600
        assert limits.max_file_bytes == 50 * MIB == 52428800
        assert limits.max_files == 2000
        assert limits.max_path_length == 255
        assert limits == DEFAULT_LIMITS

    def test_limits_are_immutable(self):
        with pytest.raises(AttributeError):
            DEFAULT_LIMITS.max_files = 1

    def test_limits_can_be_customized(self):
        limits = PackageLimits(max_files=5, max_file_bytes=10)
        assert limits.max_files == 5
        assert limits.max_file_bytes == 10
        assert limits.max_path_length == 255


class TestHappyPath:
    def test_skill_package_is_fully_described(self):
        files = {
            "scripts/run.py": "print('hi')\n",
            "assets/logo.png": PNG_BYTES,
            "data/config.json": '{"a": 1}',
            "LICENSE": "MIT",
        }
        data = make_skill_zip(
            "web-search",
            "1.2.3",
            description="Search the web.",
            tags=["Web", "Search"],
            category="Research",
            files=files,
        )
        parsed = inspect_package(data)
        assert parsed.kind == "skill"
        assert parsed.entrypoint == "SKILL.md"
        assert parsed.root_prefix == ""
        assert parsed.name == "web-search"
        assert parsed.version == "1.2.3"
        assert parsed.display_name == "Web Search"
        assert parsed.description == "Search the web."
        assert parsed.category == "research"
        assert parsed.tags == ["web", "search"]
        assert parsed.metadata == {}
        assert parsed.sha256 == sha(data)
        assert parsed.size_bytes == len(data)
        assert parsed.file_count == 5
        assert paths_of(parsed) == sorted(
            ["SKILL.md", "scripts/run.py", "assets/logo.png", "data/config.json", "LICENSE"]
        )
        assert parsed.uncompressed_bytes == sum(entry.size for entry in parsed.manifest)

    def test_grounding_package_is_deduced_from_the_index_entrypoint(self):
        parsed = inspect_package(
            make_grounding_zip(
                "company-handbook", "0.3.0", files={"tables/prices.csv": "a,b\n1,2\n"}
            )
        )
        assert parsed.kind == "grounding"
        assert parsed.entrypoint == "INDEX.md"
        assert parsed.root_prefix == ""
        assert parsed.name == "company-handbook"
        assert parsed.display_name == "Company Handbook"
        assert paths_of(parsed) == ["INDEX.md", "tables/prices.csv"]

    def test_manifest_entries_describe_the_real_content(self):
        files = {
            "scripts/run.py": "print('hi')\n",
            "assets/logo.png": PNG_BYTES,
            "data/config.json": '{"a": 1}',
            "empty.txt": "",
            "README": "plain text without extension",
        }
        entrypoint = skill_md("manifest-check", "1.0.0")
        parsed = inspect_package(make_skill_zip("manifest-check", "1.0.0", files=files))
        manifest = manifest_of(parsed)
        expected = {
            name: (content if isinstance(content, bytes) else content.encode("utf-8"))
            for name, content in files.items()
        }
        expected["SKILL.md"] = entrypoint.encode("utf-8")
        assert set(manifest) == set(expected)
        for path, content in expected.items():
            entry = manifest[path]
            assert entry.size == len(content)
            assert entry.sha256 == sha(content)
            assert isinstance(entry.text, bool)
        assert manifest["scripts/run.py"].media_type == "text/x-python"
        assert manifest["assets/logo.png"].media_type == "image/png"
        assert manifest["data/config.json"].media_type == "application/json"
        assert manifest["SKILL.md"].media_type == "text/markdown"
        assert manifest["README"].media_type == "application/octet-stream"
        assert manifest["scripts/run.py"].text is True
        assert manifest["assets/logo.png"].text is False
        assert manifest["empty.txt"].text is True
        assert manifest["empty.txt"].size == 0
        assert manifest["empty.txt"].sha256 == sha(b"")
        assert manifest["README"].text is True

    def test_manifest_is_sorted_by_code_point_not_by_directory(self):
        names = ["b.txt", "a/z.txt", "a.txt", "a-b.txt", "Z.txt", "z/a/b.txt", "_x"]
        parsed = inspect_package(make_skill_zip(files={name: "x" for name in names}))
        assert paths_of(parsed) == sorted([*names, "SKILL.md"])
        assert paths_of(parsed).index("a.txt") < paths_of(parsed).index("a/z.txt")

    def test_directory_entries_are_not_part_of_the_manifest(self):
        data = make_skill_zip(files={"a/b/c.txt": "x", "a/d.txt": "y"}, dir_entries=True)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            assert {"a/", "a/b/"} <= set(archive.namelist())
        parsed = inspect_package(data)
        assert paths_of(parsed) == ["SKILL.md", "a/b/c.txt", "a/d.txt"]
        assert parsed.file_count == 3

    @pytest.mark.parametrize("compression", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
    def test_stored_and_deflated_entries_give_identical_results(self, compression):
        files = {"data/blob.bin": bytes(range(256)) * 40, "notes.txt": "hello\n" * 100}
        reference = inspect_package(make_skill_zip(files=files, compression=zipfile.ZIP_STORED))
        parsed = inspect_package(make_skill_zip(files=files, compression=compression))
        assert parsed.manifest == reference.manifest
        assert parsed.uncompressed_bytes == reference.uncompressed_bytes

    def test_inspection_is_deterministic(self):
        data = make_skill_zip("repeatable", "1.0.0", files={"a.txt": "a"})
        assert inspect_package(data) == inspect_package(data)

    def test_the_input_bytes_are_not_modified(self):
        data = make_skill_zip("pristine", "1.0.0")
        snapshot = bytes(data)
        inspect_package(data)
        assert data == snapshot

    def test_every_manifest_entry_can_be_read_back_and_verified(self):
        files = {
            "scripts/run.py": "print('hi')\n",
            "assets/logo.png": PNG_BYTES,
            "docs/caf" + E_ACUTE + ".md": "# cafe\n",
            "deep/a/b/c/d.txt": "deep",
        }
        for wrapper in (None, "wrapped"):
            data = make_skill_zip("roundtrip", "1.0.0", files=files, wrapper=wrapper)
            parsed = inspect_package(data)
            for entry in parsed.manifest:
                content = read_package_file(data, parsed.root_prefix, entry.path)
                assert len(content) == entry.size
                assert sha(content) == entry.sha256

    def test_manifest_entry_serializes_to_the_contract_shape(self):
        entry = ManifestEntry("scripts/run.py", 12, "ab" * 32, "text/x-python", True)
        assert entry.to_dict() == {
            "path": "scripts/run.py",
            "size": 12,
            "sha256": "ab" * 32,
            "media_type": "text/x-python",
            "text": True,
        }
        assert list(entry.to_dict()) == ["path", "size", "sha256", "media_type", "text"]
        assert json.loads(json.dumps(entry.to_dict())) == entry.to_dict()

    def test_parsed_package_is_immutable(self):
        parsed = inspect_package(make_skill_zip("frozen", "1.0.0"))
        with pytest.raises(AttributeError):
            parsed.name = "other"

    def test_a_package_built_with_the_standard_library_archiver_is_accepted(self, tmp_path):
        source = tmp_path / "source" / "my-skill"
        (source / "scripts").mkdir(parents=True)
        (source / "SKILL.md").write_text(skill_md("my-skill", "1.0.0"), encoding="utf-8")
        (source / "scripts" / "run.py").write_text("print(1)\n", encoding="utf-8")
        flat = shutil.make_archive(str(tmp_path / "flat"), "zip", root_dir=source)
        wrapped = shutil.make_archive(
            str(tmp_path / "wrapped"), "zip", root_dir=tmp_path / "source", base_dir="my-skill"
        )
        flat_parsed = inspect_package(Path(flat).read_bytes())
        wrapped_parsed = inspect_package(Path(wrapped).read_bytes())
        assert flat_parsed.root_prefix == ""
        assert wrapped_parsed.root_prefix == "my-skill/"
        assert paths_of(flat_parsed) == paths_of(wrapped_parsed) == ["SKILL.md", "scripts/run.py"]
        assert flat_parsed.manifest == wrapped_parsed.manifest

    def test_zip_comment_and_extra_fields_are_tolerated(self):
        data = build_raw_zip(
            [
                stored_entry(
                    "SKILL.md", skill_md("commented", "1.0.0"), extra=b"UT\x05\x00\x03" + bytes(5)
                )
            ],
            comment=b"made by a test",
        )
        assert inspect_package(data).name == "commented"

    def test_unicode_file_names_are_preserved(self):
        name = "docs/caf" + E_ACUTE + "/" + EURO + ".md"
        parsed = inspect_package(make_skill_zip(files={name: "x"}))
        assert name in paths_of(parsed)


class TestEntrypointAndLayout:
    def test_missing_entrypoint(self):
        error = rejection(make_zip({"README.md": "hi", "scripts/run.py": "x"}))
        assert error.reason == "MISSING_ENTRYPOINT"
        assert "SKILL.md" in error.message
        assert "INDEX.md" in error.message

    def test_an_archive_without_entries_has_no_entrypoint(self):
        assert reason_of(make_zip({})) == "MISSING_ENTRYPOINT"

    def test_an_archive_with_only_directories_has_no_entrypoint(self):
        assert reason_of(build_raw_zip([stored_entry("docs/", external_attr=UNIX_DIRECTORY)])) == (
            "MISSING_ENTRYPOINT"
        )

    def test_both_entrypoints_at_the_root_are_ambiguous(self):
        data = make_zip(
            {"SKILL.md": skill_md("both", "1.0.0"), "INDEX.md": index_md("both", "1.0.0")}
        )
        assert reason_of(data) == "AMBIGUOUS_ENTRYPOINT"

    def test_both_entrypoints_inside_a_wrapper_are_ambiguous(self):
        data = make_zip(
            {"SKILL.md": skill_md("both", "1.0.0"), "INDEX.md": index_md("both", "1.0.0")},
            wrapper="pkg",
        )
        assert reason_of(data) == "AMBIGUOUS_ENTRYPOINT"

    @pytest.mark.parametrize("name", ["skill.md", "Skill.md", "SKILL.MD", "index.md", "Index.md"])
    def test_entrypoint_names_are_case_sensitive(self, name):
        assert reason_of(make_zip({name: skill_md("case", "1.0.0")})) == "MISSING_ENTRYPOINT"

    def test_entrypoints_in_subfolders_are_ordinary_files(self):
        data = make_zip({"docs/SKILL.md": skill_md("nested", "1.0.0"), "notes.txt": "x"})
        assert reason_of(data) == "MISSING_ENTRYPOINT"

    def test_nested_entrypoint_names_do_not_make_the_package_ambiguous(self):
        data = make_skill_zip(
            "outer", "1.0.0", files={"docs/INDEX.md": "# index", "docs/SKILL.md": "# nested"}
        )
        parsed = inspect_package(data)
        assert parsed.kind == "skill"
        assert {"docs/INDEX.md", "docs/SKILL.md"} <= set(paths_of(parsed))

    def test_an_index_in_a_subfolder_does_not_turn_a_skill_into_a_grounding(self):
        parsed = inspect_package(make_skill_zip("outer", "1.0.0", files={"pkg/INDEX.md": "x"}))
        assert parsed.kind == "skill"
        assert parsed.root_prefix == ""

    def test_a_directory_named_like_an_entrypoint_is_not_an_entrypoint(self):
        data = build_raw_zip(
            [
                stored_entry("SKILL.md/", external_attr=UNIX_DIRECTORY),
                stored_entry("SKILL.md/inner.txt", "x"),
            ]
        )
        assert reason_of(data) == "MISSING_ENTRYPOINT"

    def test_wrapper_directory_is_detected_and_stripped(self):
        data = make_skill_zip(
            "wrapped", "1.0.0", files={"scripts/run.py": "x", "docs/a.md": "y"}, wrapper="my-pkg"
        )
        parsed = inspect_package(data)
        assert parsed.root_prefix == "my-pkg/"
        assert parsed.entrypoint == "SKILL.md"
        assert paths_of(parsed) == ["SKILL.md", "docs/a.md", "scripts/run.py"]

    def test_wrapper_with_explicit_directory_entries(self):
        data = make_skill_zip(
            "wrapped", "1.0.0", files={"scripts/run.py": "x"}, wrapper="pkg", dir_entries=True
        )
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            assert "pkg/" in archive.namelist()
        parsed = inspect_package(data)
        assert parsed.root_prefix == "pkg/"
        assert paths_of(parsed) == ["SKILL.md", "scripts/run.py"]

    def test_wrapped_grounding_package(self):
        parsed = inspect_package(make_grounding_zip("kb", "1.0.0", wrapper="kb-folder"))
        assert parsed.kind == "grounding"
        assert parsed.root_prefix == "kb-folder/"
        assert parsed.entrypoint == "INDEX.md"

    def test_a_wrapped_and_an_unwrapped_package_share_the_same_manifest(self):
        files = {"scripts/run.py": "x", "docs/a.md": "y"}
        flat = inspect_package(make_skill_zip("same", "1.0.0", files=files))
        wrapped = inspect_package(make_skill_zip("same", "1.0.0", files=files, wrapper="same"))
        assert flat.manifest == wrapped.manifest
        assert flat.uncompressed_bytes == wrapped.uncompressed_bytes
        assert flat.root_prefix == ""
        assert wrapped.root_prefix == "same/"

    @pytest.mark.parametrize("wrapper", ["my skill", "caf" + E_ACUTE, "a.b", "x" * 200])
    def test_wrapper_names_may_contain_spaces_unicode_and_dots(self, wrapper):
        parsed = inspect_package(make_skill_zip("wrapped", "1.0.0", wrapper=wrapper))
        assert parsed.root_prefix == wrapper + "/"
        assert paths_of(parsed) == ["SKILL.md"]

    def test_only_one_wrapper_level_is_removed(self):
        data = make_skill_zip("deep", "1.0.0", wrapper="outer/inner")
        assert reason_of(data) == "MISSING_ENTRYPOINT"

    def test_two_top_level_directories_are_not_a_wrapper(self):
        data = make_zip(
            {"a/SKILL.md": skill_md("two", "1.0.0"), "b/SKILL.md": skill_md("two", "1.0.0")}
        )
        assert reason_of(data) == "MISSING_ENTRYPOINT"

    def test_a_second_top_level_directory_without_an_entrypoint_is_not_a_wrapper_either(self):
        data = make_zip({"a/SKILL.md": skill_md("two", "1.0.0"), "b/data.txt": "x"})
        assert reason_of(data) == "MISSING_ENTRYPOINT"

    @pytest.mark.parametrize("order", [("a", "b"), ("b", "a")], ids=["a-first", "b-first"])
    @pytest.mark.parametrize(
        "entrypoints",
        [("SKILL.md", "SKILL.md"), ("INDEX.md", "INDEX.md"), ("SKILL.md", "INDEX.md")],
        ids=["two-skills", "two-indexes", "skill-and-index"],
    )
    def test_two_top_level_directories_that_both_hold_an_entrypoint_are_not_a_wrapper(
        self, order, entrypoints
    ):
        files = {
            f"{directory}/{entrypoint}": skill_md(f"dir-{directory}", "1.0.0")
            for directory, entrypoint in zip(("a", "b"), entrypoints, strict=True)
        }
        ordered = {path: files[path] for path in sorted(files, key=lambda p: order.index(p[0]))}
        assert sorted(path[0] for path in ordered) == ["a", "b"]
        assert reason_of(make_zip(ordered)) == "MISSING_ENTRYPOINT"

    def test_the_wrapper_decision_does_not_depend_on_the_hash_seed(self):
        script = (
            "import sys\n"
            "from app.services.package_inspector import inspect_package\n"
            "from app.services.errors import InvalidPackageError\n"
            "from tests.helpers.packages import make_zip, skill_md\n"
            "data = make_zip({'a/SKILL.md': skill_md('two', '1.0.0'),"
            " 'b/SKILL.md': skill_md('two', '1.0.0')})\n"
            "try:\n"
            "    inspect_package(data)\n"
            "except InvalidPackageError as error:\n"
            "    print(error.reason)\n"
            "else:\n"
            "    print('ACCEPTED')\n"
        )
        project = Path(inspector.__file__).resolve().parents[2]
        outcomes = set()
        for seed in ("0", "1", "2", "3", "7", "42", "1234", "99999"):
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=project,
                env={**os.environ, "PYTHONHASHSEED": seed},
                capture_output=True,
                text=True,
                check=True,
                timeout=120,
            )
            outcomes.add(result.stdout.strip())
        assert outcomes == {"MISSING_ENTRYPOINT"}

    def test_a_root_level_file_prevents_wrapper_detection(self):
        data = make_zip({"pkg/SKILL.md": skill_md("mixed", "1.0.0"), "README.md": "hi"})
        assert reason_of(data) == "MISSING_ENTRYPOINT"

    def test_a_root_entrypoint_wins_over_a_single_top_level_directory(self):
        data = make_zip(
            {"SKILL.md": skill_md("flat", "1.0.0"), "pkg/INDEX.md": "x", "pkg/a.txt": "y"}
        )
        parsed = inspect_package(data)
        assert parsed.root_prefix == ""
        assert parsed.kind == "skill"
        assert "pkg/INDEX.md" in paths_of(parsed)

    def test_a_file_named_like_the_wrapper_directory_is_not_a_wrapper(self):
        data = build_raw_zip(
            [
                stored_entry("pkg", "a file called pkg"),
                stored_entry("pkg/SKILL.md", skill_md("clash", "1.0.0")),
            ]
        )
        assert reason_of(data) == "DUPLICATE_PATH"


class TestIgnoredEntries:
    def test_macos_resource_forks_and_ds_store_are_dropped(self):
        data = make_skill_zip(
            "mac", "1.0.0", files={"scripts/run.py": "x"}, wrapper="mac-pkg", macos_junk=True
        )
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
        assert "__MACOSX/" in names
        assert "mac-pkg/.DS_Store" in names
        assert any(name.startswith("__MACOSX/mac-pkg/._") for name in names)
        parsed = inspect_package(data)
        assert parsed.root_prefix == "mac-pkg/"
        assert paths_of(parsed) == ["SKILL.md", "scripts/run.py"]
        assert parsed.file_count == 2

    def test_junk_outside_the_wrapper_does_not_break_wrapper_detection(self):
        data = make_zip(
            {
                "pkg/SKILL.md": skill_md("junky", "1.0.0"),
                "__MACOSX/pkg/._SKILL.md": b"apple double",
                ".DS_Store": b"root store",
                "Thumbs.db": b"thumbs",
            }
        )
        parsed = inspect_package(data)
        assert parsed.root_prefix == "pkg/"
        assert paths_of(parsed) == ["SKILL.md"]

    def test_ds_store_and_thumbs_db_are_ignored_at_any_depth(self):
        data = make_skill_zip(
            files={
                "docs/.DS_Store": b"x",
                "a/b/c/.DS_Store": b"x",
                "assets/Thumbs.db": b"x",
                "Thumbs.db": b"x",
                "docs/keep.md": "keep",
            }
        )
        assert paths_of(inspect_package(data)) == ["SKILL.md", "docs/keep.md"]

    def test_similar_looking_names_are_regular_files(self):
        data = make_skill_zip(
            files={
                ".ds_store": "x",
                "thumbs.db": "x",
                "docs/__MACOSX/x.txt": "x",
                "__MACOSX_notes.txt": "x",
                "__MACOSX": "a plain file with that name",
            }
        )
        assert set(paths_of(inspect_package(data))) == {
            "SKILL.md",
            ".ds_store",
            "thumbs.db",
            "docs/__MACOSX/x.txt",
            "__MACOSX_notes.txt",
            "__MACOSX",
        }

    def test_ignored_entries_do_not_count_toward_any_limit(self):
        junk = {f"__MACOSX/._f{index}": bytes(300 * KIB) for index in range(10)}
        junk[".DS_Store"] = bytes(300 * KIB)
        data = make_skill_zip(files={"a.txt": "x", **junk})
        limits = PackageLimits(max_files=2, max_file_bytes=1 * KIB, max_uncompressed_bytes=2 * KIB)
        parsed = inspect_package(data, limits)
        assert parsed.file_count == 2
        assert parsed.uncompressed_bytes < 2 * KIB

    def test_an_entry_with_conflicting_names_is_never_ignored(self):
        extra = unicode_path_extra("__MACOSX/innocent", "evil.txt")
        data = build_raw_zip(
            [
                stored_entry("SKILL.md", skill_md("sneaky", "1.0.0")),
                stored_entry("__MACOSX/innocent", "payload", extra=extra),
            ]
        )
        assert reason_of(data) == "UNSAFE_PATH"


def base_entries():
    return [
        layout_stored("SKILL.md", skill_md("probe", "1.0.0")),
        layout_stored("a.txt", "alpha"),
        layout_stored("b.txt", "bravo"),
    ]


def declare_descriptor(entry, descriptor=None):
    entry.streamed = True
    if descriptor is not None:
        entry.descriptor = descriptor
    return entry


IGNORED_NAMES = [
    "__MACOSX/._a.txt",
    "__MACOSX/docs/._a.txt",
    ".DS_Store",
    "docs/.DS_Store",
    "Thumbs.db",
    "assets/Thumbs.db",
]

UNSAFE_IGNORED_PATHS = [
    "__MACOSX/../../x",
    "../../.DS_Store",
    "__MACOSX/..",
    "__MACOSX/./x",
    "__MACOSX//x",
    "__MACOSX/.../x",
    "a/../.DS_Store",
    "../Thumbs.db",
    "/.DS_Store",
    "C:/.DS_Store",
    "__MACOSX/._a\\b",
    "__MACOSX/._a\nb",
    "__MACOSX/._a" + chr(0x202E) + "b",
    "__MACOSX/" + "x" * 300,
    "dir/" + "y" * 300 + "/.DS_Store",
]


MACOS_FILES = {
    "SKILL.md": skill_md("mac-made", "1.0.0"),
    "scripts/run.py": "print(1)\n",
    "docs/readme.md": "# readme\n",
    "assets/logo.png": PNG_BYTES,
}


class TestIgnoredEntriesAreChecked:
    @staticmethod
    def package_with(entry):
        return build_layout_zip([*base_entries(), entry])

    @pytest.mark.parametrize("name", IGNORED_NAMES)
    def test_the_control_package_drops_the_harmless_entry(self, name):
        parsed = inspect_package(self.package_with(layout_stored(name, "harmless")))
        assert paths_of(parsed) == ["SKILL.md", "a.txt", "b.txt"]
        assert parsed.file_count == 3

    @pytest.mark.parametrize("name", IGNORED_NAMES)
    def test_a_symbolic_link_under_an_ignored_name_is_rejected(self, name):
        entry = layout_stored(name, "/etc/passwd", external_attr=UNIX_SYMLINK)
        error = rejection(self.package_with(entry))
        assert error.reason == "SYMLINK_NOT_ALLOWED"
        assert name.rpartition("/")[2] in error.message

    @pytest.mark.parametrize("name", IGNORED_NAMES)
    @pytest.mark.parametrize("flags", [0x1, 0x40, 0x41, 0x9])
    def test_an_encrypted_ignored_entry_is_rejected(self, name, flags):
        entry = layout_stored(name, b"\x8f" * 24, flags=flags)
        assert reason_of(self.package_with(entry)) == "ENCRYPTED_ENTRY"

    @pytest.mark.parametrize("name", IGNORED_NAMES)
    @pytest.mark.parametrize("method", [1, 12, 14, 99])
    def test_an_ignored_entry_with_an_unsupported_method_is_rejected(self, name, method):
        entry = layout_stored(name, b"x" * 20, method=method)
        error = rejection(self.package_with(entry))
        assert error.reason == "UNSUPPORTED_COMPRESSION"
        assert str(method) in error.message

    @pytest.mark.parametrize("path", UNSAFE_IGNORED_PATHS)
    def test_unsafe_paths_that_look_ignored_are_rejected(self, path):
        assert inspector._is_ignored_name(path)
        error = rejection(self.package_with(layout_stored(path, "x")))
        assert error.reason == "UNSAFE_PATH"

    @pytest.mark.parametrize("path", UNSAFE_IGNORED_PATHS)
    def test_unsafe_directory_paths_that_look_ignored_are_rejected(self, path):
        assert reason_of(self.package_with(layout_directory(path))) == "UNSAFE_PATH"

    def test_the_two_documented_traversals_are_rejected(self):
        for path in ("__MACOSX/../../x", "../../.DS_Store"):
            assert reason_of(self.package_with(layout_stored(path, "payload"))) == "UNSAFE_PATH"

    @pytest.mark.parametrize(
        "name",
        [
            "__MACOSX/._ok\x00../../evil",
            ".DS_Store\x00",
            "docs/Thumbs.db\x00/../../x",
            "__MACOSX/\x00",
        ],
    )
    def test_a_nul_byte_in_an_ignored_name_is_rejected(self, name):
        error = rejection(self.package_with(layout_stored(name, "x")))
        assert error.reason == "UNSAFE_PATH"
        assert "NUL" in error.message

    @pytest.mark.parametrize("name", IGNORED_NAMES)
    def test_a_unicode_path_field_that_renames_an_ignored_entry_is_rejected(self, name):
        extra = unicode_path_extra(name, "../../evil.sh")
        entry = layout_stored(name, "x", extra=extra)
        with zipfile.ZipFile(io.BytesIO(self.package_with(entry))) as archive:
            info = archive.infolist()[-1]
            assert (info.orig_filename, info.filename) == (name, "../../evil.sh")
        error = rejection(self.package_with(entry))
        assert error.reason == "UNSAFE_PATH"
        assert "conflicting" in error.message

    @pytest.mark.parametrize(
        "name", ["__MACOSX/", "__MACOSX/docs/", "docs/.DS_Store/", "Thumbs.db/"]
    )
    def test_directory_entries_with_ignored_names_are_checked_too(self, name):
        control = inspect_package(self.package_with(layout_directory(name)))
        assert paths_of(control) == ["SKILL.md", "a.txt", "b.txt"]
        cases = {
            "SYMLINK_NOT_ALLOWED": layout_directory(name, external_attr=UNIX_SYMLINK),
            "ENCRYPTED_ENTRY": layout_directory(name, flags=0x1),
            "UNSUPPORTED_COMPRESSION": layout_directory(name, method=12),
            "CORRUPT_ARCHIVE": LayoutEntry(name, b"hidden payload", external_attr=UNIX_DIRECTORY),
        }
        for reason, entry in cases.items():
            assert reason_of(self.package_with(entry)) == reason

    def test_ignored_entries_stay_out_of_the_manifest_and_are_never_hashed(self, monkeypatch):
        opened = []
        original_open = zipfile.ZipFile.open

        def recording_open(self, name, *args, **kwargs):
            opened.append(getattr(name, "orig_filename", name))
            return original_open(self, name, *args, **kwargs)

        counter = CountingHashlib()
        monkeypatch.setattr(zipfile.ZipFile, "open", recording_open)
        monkeypatch.setattr(inspector, "hashlib", counter)
        lying = [
            layout_deflated("__MACOSX/._wrong_crc", "payload", crc=0xDEADBEEF),
            layout_deflated(".DS_Store", "x" * 5000, uncompressed_size=1),
            layout_stored("Thumbs.db", "short", uncompressed_size=999),
            layout_stored("__MACOSX/._stored", "short", crc=0xDEADBEEF),
        ]
        parsed = inspect_package(build_layout_zip([*base_entries(), *lying]))
        assert paths_of(parsed) == ["SKILL.md", "a.txt", "b.txt"]
        assert sorted(opened) == ["SKILL.md", "a.txt", "b.txt"]
        assert counter.hashed == parsed.uncompressed_bytes + parsed.size_bytes

    @pytest.mark.parametrize("name", [".DS_Store", "__MACOSX/._a.txt", "docs/"])
    def test_an_ignored_or_directory_entry_that_is_not_a_deflate_stream_is_rejected(self, name):
        garbage = LayoutEntry(
            name,
            b"\xff" * 40,
            method=zipfile.ZIP_DEFLATED,
            uncompressed_size=0,
            crc=0,
            external_attr=UNIX_DIRECTORY if name.endswith("/") else 0o100644 << 16,
        )
        error = rejection(build_layout_zip([*base_entries(), garbage]))
        assert error.reason == "CORRUPT_ARCHIVE"
        assert name in error.message

    def test_an_ignored_bomb_is_refused_after_a_bounded_amount_of_inflation(self, monkeypatch):
        counter = CountingZlib()
        monkeypatch.setattr(inspector, "zlib", counter)
        data = build_layout_zip([*base_entries(), layout_bomb("__MACOSX/._bomb", 3 * GIB)])
        tracemalloc.start()
        started = time.perf_counter()
        try:
            error = rejection(data)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert error.reason == "TOO_LARGE"
        assert "ignored" in error.message
        assert inspector._UNREAD_STREAM_BYTES == 64 * MIB
        assert str(64 * MIB) in error.message
        assert 32 * MIB < counter.inflated <= 64 * MIB + 64 * KIB
        assert time.perf_counter() - started < 3
        assert peak < 8 * MIB

    def test_the_inflation_budget_of_ignored_entries_is_shared_and_exact(self, monkeypatch):
        monkeypatch.setattr(inspector, "_UNREAD_STREAM_BYTES", 1 * MIB)

        def junk(*sizes):
            entries = [
                layout_deflated(f"__MACOSX/._junk{index}", bytes(size))
                for index, size in enumerate(sizes)
            ]
            return build_layout_zip([*base_entries(), *entries])

        assert inspect_package(junk(512 * KIB, 512 * KIB)).file_count == 3
        assert inspect_package(junk(1 * MIB)).file_count == 3
        error = rejection(junk(512 * KIB, 512 * KIB, 1))
        assert (error.reason, "ignored" in error.message) == ("TOO_LARGE", True)
        assert reason_of(junk(1 * MIB + 1)) == "TOO_LARGE"
        assert reason_of(junk(*([100 * KIB] * 11))) == "TOO_LARGE"

    def test_ignored_entries_do_not_spend_the_budget_of_the_package(self):
        junk = {f"__MACOSX/._f{index}": bytes(200 * KIB) for index in range(5)}
        data = make_skill_zip(files={"a.txt": "x", **junk})
        limits = PackageLimits(max_uncompressed_bytes=1 * KIB, max_file_bytes=1 * KIB)
        assert inspect_package(data, limits).uncompressed_bytes < 1 * KIB

    def test_the_junk_written_by_the_helper_passes_every_check(self):
        data = make_skill_zip(
            files={"a.txt": "x", "docs/b.txt": "y"},
            wrapper="pkg",
            dir_entries=True,
            macos_junk=True,
        )
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
        assert "__MACOSX/" in names and "pkg/.DS_Store" in names
        assert paths_of(inspect_package(data)) == ["SKILL.md", "a.txt", "docs/b.txt"]


class TestMacosArchives:
    def test_the_fixture_is_built_like_an_archive_utility_zip(self):
        data = make_macos_zip(MACOS_FILES, wrapper="mac-made")
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = {info.orig_filename: info for info in archive.infolist()}
            fork = archive.read("__MACOSX/mac-made/._SKILL.md")
        assert {"mac-made/", "__MACOSX/", "__MACOSX/mac-made/"} <= set(infos)
        assert "__MACOSX/mac-made/._SKILL.md" in infos
        assert "__MACOSX/mac-made/scripts/._run.py" in infos
        assert "mac-made/.DS_Store" in infos
        files = [info for name, info in infos.items() if not name.endswith("/")]
        assert files and all(info.flag_bits & DESCRIPTOR_FLAG for info in files)
        assert all(info.compress_type == zipfile.ZIP_DEFLATED for info in files)
        assert all((info.external_attr >> 16) & 0o170000 == 0o100000 for info in files)
        local_offset = local_header_offset(data, "mac-made/SKILL.md")
        local_extra = struct.unpack_from("<H", data, local_offset + 28)[0]
        assert local_extra != len(infos["mac-made/SKILL.md"].extra)
        assert fork.startswith(APPLE_DOUBLE_BODY[:4])

    def test_a_macos_archive_is_accepted_and_the_junk_never_reaches_the_manifest(self):
        data = make_macos_zip(MACOS_FILES, wrapper="mac-made")
        parsed = inspect_package(data)
        clean = inspect_package(make_zip(MACOS_FILES, wrapper="mac-made"))
        assert parsed.root_prefix == "mac-made/"
        assert parsed.manifest == clean.manifest
        assert paths_of(parsed) == sorted(MACOS_FILES)
        assert parsed.file_count == len(MACOS_FILES)
        for entry in parsed.manifest:
            content = read_package_file(data, parsed.root_prefix, entry.path)
            assert sha(content) == entry.sha256

    def test_a_macos_archive_with_a_comment_is_accepted(self):
        data = make_macos_zip(MACOS_FILES, wrapper="mac-made", comment=b"zipped by Finder")
        assert inspect_package(data).root_prefix == "mac-made/"

    def test_a_macos_archive_without_a_wrapper_directory_is_accepted(self):
        entries = [
            layout_stored("SKILL.md", MACOS_FILES["SKILL.md"]),
            layout_directory("__MACOSX"),
            layout_deflated("__MACOSX/._SKILL.md", APPLE_DOUBLE_BODY, streamed=True),
            layout_deflated(".DS_Store", DS_STORE_BODY, streamed=True),
        ]
        parsed = inspect_package(build_layout_zip(entries))
        assert parsed.root_prefix == "" and paths_of(parsed) == ["SKILL.md"]

    def test_the_same_archive_with_a_hostile_resource_fork_is_rejected(self):
        data = make_macos_zip(MACOS_FILES, wrapper="mac-made")
        assert inspect_package(data).file_count == len(MACOS_FILES)
        entries = [
            layout_stored("mac-made/SKILL.md", MACOS_FILES["SKILL.md"]),
            layout_stored(
                "__MACOSX/mac-made/._SKILL.md", "/etc/passwd", external_attr=UNIX_SYMLINK
            ),
        ]
        assert reason_of(build_layout_zip(entries)) == "SYMLINK_NOT_ALLOWED"


class TestFrontmatterFormat:
    def test_minimal_frontmatter(self):
        parsed = inspect_block(VALID_BLOCK)
        assert (parsed.name, parsed.description, parsed.version) == (
            "sample",
            "Sample description",
            "1.0.0",
        )

    def test_utf8_byte_order_mark_is_accepted(self):
        text = frontmatter_doc(VALID_BLOCK, "# Body\n", bom=True)
        assert text.startswith(chr(0xFEFF))
        parsed = inspect_text(text)
        assert parsed.name == "sample"
        assert parsed.search_text == "# Body"

    def test_the_raw_bom_bytes_are_accepted(self):
        data = make_zip({"SKILL.md": b"\xef\xbb\xbf" + frontmatter_doc(VALID_BLOCK, "x").encode()})
        assert inspect_package(data).name == "sample"

    def test_crlf_line_endings_are_accepted(self):
        text = frontmatter_doc(VALID_BLOCK, "Line one\nLine two\n", eol="\r\n")
        assert "\r\n" in text
        parsed = inspect_text(text)
        assert parsed.name == "sample"
        assert parsed.description == "Sample description"
        assert parsed.search_text == "Line one\r\nLine two"

    def test_crlf_and_bom_together(self):
        parsed = inspect_text(frontmatter_doc(VALID_BLOCK, "body\n", eol="\r\n", bom=True))
        assert parsed.name == "sample"

    def test_trailing_whitespace_on_the_delimiters_is_accepted(self):
        text = "---  \t\n" + VALID_BLOCK + "---   \nbody\n"
        assert inspect_text(text).name == "sample"

    def test_closing_delimiter_at_the_end_of_the_file(self):
        assert inspect_text("---\n" + VALID_BLOCK + "---").name == "sample"
        assert inspect_text("---\n" + VALID_BLOCK + "---\n").search_text == ""

    def test_yaml_comments_and_blank_lines_inside_the_block(self):
        block = "# leading comment\n\nname: sample\n\n# another\ndescription: d  # trailing\nversion: 1.0.0\n"
        parsed = inspect_block(block)
        assert parsed.description == "d"

    def test_horizontal_rules_in_the_body_do_not_confuse_the_parser(self):
        body = "Intro\n\n---\n\nMiddle\n\n---\nname: not-frontmatter\n---\n"
        parsed = inspect_block(VALID_BLOCK, body)
        assert parsed.name == "sample"
        assert "Middle" in parsed.search_text
        assert "not-frontmatter" in parsed.search_text

    def test_only_the_first_closing_delimiter_ends_the_block(self):
        parsed = inspect_block(VALID_BLOCK, "---\nextra: ignored\n---\nrest")
        assert "extra" not in parsed.metadata

    def test_an_empty_frontmatter_reports_the_missing_name(self):
        error = rejection(make_zip({"SKILL.md": "---\n---\nbody"}), version="1.0.0")
        assert error.reason == "MISSING_FIELD"
        assert "name" in error.message

    def test_a_comment_only_frontmatter_reports_the_missing_name(self):
        error = rejection(make_zip({"SKILL.md": "---\n# nothing\n---\n"}), version="1.0.0")
        assert error.reason == "MISSING_FIELD"

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "# Just a title\n\nNo frontmatter here.\n",
            "\n---\nname: a\n---\n",
            " ---\nname: a\n---\n",
            "text\n---\nname: a\n---\n",
            "----\nname: a\n----\n",
            "--- name: a\n---\n",
            "---name: a\n---\n",
            "---",
            "---\n",
            "---\nname: a\n",
            "---\nname: a\ndescription: d\nversion: 1.0.0\n",
            "---\nname: a\n---x\n",
            "---\nname: a\n----\n",
            "---\nname: a\n...\n",
            "---\nname: a\n- --\n",
        ],
    )
    def test_malformed_frontmatter_structure(self, text):
        error = rejection(make_zip({"SKILL.md": text}), version="1.0.0")
        assert error.reason == "BAD_FRONTMATTER"

    def test_the_unterminated_message_names_the_entrypoint(self):
        error = rejection(make_zip({"INDEX.md": "---\nname: a\n"}), version="1.0.0")
        assert error.reason == "BAD_FRONTMATTER"
        assert "INDEX.md" in error.message

    @pytest.mark.parametrize(
        "block",
        [
            "- a\n- b\n",
            "just a scalar\n",
            "42\n",
            "[name, description]\n",
            "true\n",
            "2024-01-01\n",
            '"quoted"\n',
        ],
    )
    def test_the_frontmatter_must_be_a_mapping(self, block):
        error = block_rejection(block)
        assert error.reason == "BAD_FRONTMATTER"
        assert "mapping" in error.message

    @pytest.mark.parametrize(
        "extra",
        ["1: one\n", "true: yes\n", "null: nothing\n", "2024-01-01: dated\n", "3.5: float\n"],
    )
    def test_top_level_keys_must_be_strings(self, extra):
        error = block_rejection(VALID_BLOCK + extra)
        assert error.reason == "BAD_FRONTMATTER"
        assert "keys" in error.message

    def test_a_complex_key_is_rejected(self):
        assert block_rejection(VALID_BLOCK + "? [a, b]\n: value\n").reason == "BAD_FRONTMATTER"

    @pytest.mark.parametrize(
        "block",
        [
            "name: [unclosed\n",
            "name: a\n  description: misindented\n",
            "name: a\n\tdescription: tab indented\n",
            "name: a\ndescription: 'unterminated\n",
            'name: a\ndescription: "unterminated\n',
            "name: a: b\n",
            "name: @reserved\n",
            "name: a\n--- # second\ndescription: second document\n",
            "name: a\n%DIRECTIVE\n",
        ],
    )
    def test_invalid_yaml_syntax(self, block):
        error = block_rejection(block)
        assert error.reason == "BAD_FRONTMATTER"
        assert "YAML" in error.message

    def test_yaml_error_messages_point_at_the_line(self):
        error = block_rejection("name: a\ndescription: d\nbroken: [unclosed\n")
        assert error.reason == "BAD_FRONTMATTER"
        assert "line" in error.message

    def test_the_frontmatter_limit_is_sixteen_kibibytes(self):
        prefix = "name: sample\ndescription: d\nversion: 1.0.0\nfiller: "
        at_limit = prefix + "a" * (16 * KIB - len(prefix) - 1) + "\n"
        assert len(at_limit.encode("utf-8")) == 16 * KIB
        assert inspect_block(at_limit).name == "sample"
        over_limit = prefix + "a" * (16 * KIB - len(prefix)) + "\n"
        assert len(over_limit.encode("utf-8")) == 16 * KIB + 1
        error = block_rejection(over_limit)
        assert error.reason == "BAD_FRONTMATTER"
        assert "16384" in error.message

    def test_the_limit_counts_bytes_not_characters(self):
        prefix = "name: sample\ndescription: d\nversion: 1.0.0\nfiller: "
        padding = (16 * KIB - len(prefix) - 1) // 3
        block = prefix + EURO * padding + "\n"
        assert len(block) < 16 * KIB < len(block.encode("utf-8")) + 3
        over = prefix + EURO * (padding + 1) + "\n"
        assert len(over.encode("utf-8")) > 16 * KIB
        assert block_rejection(over).reason == "BAD_FRONTMATTER"

    def test_a_huge_frontmatter_is_rejected_quickly(self):
        block = VALID_BLOCK + "filler: " + "x" * (2 * MIB) + "\n"
        started = time.perf_counter()
        assert block_rejection(block).reason == "BAD_FRONTMATTER"
        assert time.perf_counter() - started < 2

    def test_an_unterminated_huge_frontmatter_is_rejected_quickly(self):
        text = "---\n" + VALID_BLOCK + "filler: " + "x" * (3 * MIB)
        started = time.perf_counter()
        assert reason_of(make_zip({"SKILL.md": text}), version="1.0.0") == "BAD_FRONTMATTER"
        assert time.perf_counter() - started < 2

    def test_deeply_nested_flow_collections_are_rejected_without_burning_cpu(self):
        for opener, closer in (("[", "]"), ("{a: ", "}")):
            block = VALID_BLOCK + "deep: " + opener * 3000 + closer * 3000 + "\n"
            started = time.perf_counter()
            error = block_rejection(block)
            assert error.reason == "BAD_FRONTMATTER"
            assert "deeply" in error.message
            assert time.perf_counter() - started < 1

    def test_unclosed_deep_nesting_is_rejected_without_burning_cpu(self):
        started = time.perf_counter()
        assert block_rejection(VALID_BLOCK + "deep: " + "[" * 15000).reason == "BAD_FRONTMATTER"
        assert time.perf_counter() - started < 1

    def test_the_flow_nesting_limit_is_thirty_two_levels(self):
        at_limit = VALID_BLOCK + "nested: " + "[" * 32 + "]" * 32 + "\n"
        over_limit = VALID_BLOCK + "nested: " + "[" * 33 + "]" * 33 + "\n"
        assert "nested" in inspect_block(at_limit).metadata
        assert "deeply" in block_rejection(over_limit).message

    def test_brackets_inside_text_do_not_count_as_nesting_when_balanced(self):
        block = VALID_BLOCK + 'pattern: "[[[[[[[[[[]]]]]]]]]]"\nflat: [[1], [2], [3]]\n'
        assert inspect_block(block).metadata["flat"] == [[1], [2], [3]]

    def test_reasonable_flow_nesting_is_accepted(self):
        block = VALID_BLOCK + "nested: " + "[" * 10 + "1" + "]" * 10 + "\n"
        parsed = inspect_block(block)
        assert parsed.metadata["nested"][0][0][0][0][0][0][0][0][0][0] == 1

    def test_deeply_nested_block_sequences_hit_the_recursion_guard(self):
        block = VALID_BLOCK + "deep: " + "- " * 3000 + "x\n"
        error = block_rejection(block)
        assert error.reason == "BAD_FRONTMATTER"

    def test_deeply_nested_top_level_sequence_reports_recursion(self):
        error = block_rejection("- " * 3000 + "x\n")
        assert error.reason == "BAD_FRONTMATTER"
        assert "recursion" in error.message.lower()

    def test_integers_beyond_the_python_digit_limit_are_a_frontmatter_error(self):
        error = block_rejection(VALID_BLOCK + "huge: " + "9" * 5000 + "\n")
        assert error.reason == "BAD_FRONTMATTER"

    @pytest.mark.parametrize(
        "value", ["2023-13-45", "2023-02-31", "2023-00-10", "2024-01-01 25:61:61"]
    )
    def test_impossible_dates_are_a_frontmatter_error(self, value):
        error = block_rejection(VALID_BLOCK + f"released: {value}\n")
        assert error.reason == "BAD_FRONTMATTER"

    @pytest.mark.parametrize(
        "tag",
        [
            "!!python/object/apply:os.system",
            "!!python/object/apply:subprocess.check_output",
            "!!python/name:os.system",
            "!!python/object:builtins.object",
            "!!python/module:os",
            "!!python/tuple",
            "!!ruby/object:Object",
        ],
    )
    def test_python_object_tags_are_rejected_and_nothing_is_executed(self, tag, tmp_path):
        sentinel = tmp_path / "pwned"
        block = VALID_BLOCK + f"payload: {tag} ['touch {sentinel}']\n"
        error = block_rejection(block)
        assert error.reason == "BAD_FRONTMATTER"
        assert not sentinel.exists()

    def test_raw_control_characters_in_the_block_are_rejected(self):
        block = VALID_BLOCK + "note: a" + chr(7) + "b\n"
        data = make_zip({"SKILL.md": frontmatter_doc(block)})
        assert reason_of(data, version="1.0.0") == "BAD_FRONTMATTER"

    @pytest.mark.parametrize(
        "escape", ["\\x00", "\\x07", "\\e", "\\x7f", "\\x9b", "\\uD800", "\\uFFFE"]
    )
    def test_escaped_control_characters_are_rejected_in_text_fields(self, escape):
        for field in ("description", "display_name"):
            block = VALID_BLOCK.replace("description: Sample description\n", "")
            block += 'description: "ok"\n'
            block += f'{field}: "bad {escape} char"\n' if field == "display_name" else ""
            if field == "description":
                block = block.replace('description: "ok"', f'description: "bad {escape} char"')
            error = block_rejection(block)
            assert error.reason == "INVALID_FIELD"

    @pytest.mark.parametrize("escape", ["\\x00", "\\e", "\\uD800"])
    def test_escaped_control_characters_are_rejected_in_metadata(self, escape):
        error = block_rejection(VALID_BLOCK + f'note: "bad {escape} char"\n')
        assert error.reason == "BAD_FRONTMATTER"
        error = block_rejection(VALID_BLOCK + f'"bad {escape} key": value\n')
        assert error.reason == "BAD_FRONTMATTER"

    def test_tabs_newlines_and_unicode_are_fine_in_text_fields(self):
        block = VALID_BLOCK.replace(
            "description: Sample description\n",
            'description: "line one\\nline\\ttwo ' + EURO + '"\n',
        )
        parsed = inspect_block(block)
        assert parsed.description == "line one\nline\ttwo " + EURO


class TestSplitFrontmatter:
    @pytest.mark.parametrize(
        ("text", "block", "body"),
        [
            ("---\nname: a\n---\nbody\n", "name: a\n", "body\n"),
            ("---\r\nname: a\r\n---\r\nbody\r\n", "name: a\r\n", "body\r\n"),
            ("---  \t\nname: a\n---   \nbody", "name: a\n", "body"),
            ("---\nname: a\n---", "name: a\n", ""),
            ("---\nname: a\n---\n", "name: a\n", ""),
            ("---\n---\nbody", "", "body"),
            ("---\na: b\n----\n---\nbody", "a: b\n----\n", "body"),
            ("---\na: b\n---\nx\n---\ny: z\n---\n", "a: b\n", "x\n---\ny: z\n---\n"),
            ("---\nname: caf\u00e9\n---\n\u20ac", "name: caf\u00e9\n", "\u20ac"),
        ],
    )
    def test_the_block_and_the_body_are_cut_at_the_delimiter_lines(self, text, block, body):
        assert split_frontmatter(text) == (block, body)

    def test_the_two_parts_rebuild_the_text_around_the_delimiters(self):
        text = "---\nname: a\nversion: 1.0.0\n---\n# Title\n\nBody\n"

        block, body = split_frontmatter(text)

        assert text == f"---\n{block}---\n{body}"

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "# Just a title\n",
            "\n---\nname: a\n---\n",
            " ---\nname: a\n---\n",
            "text\n---\nname: a\n---\n",
            "----\nname: a\n----\n",
            "---name: a\n---\n",
            "---",
            "---\n",
            "---\nname: a\n",
            "---\nname: a\n---x\n",
            "---\nname: a\n...\n",
            chr(0xFEFF) + "---\nname: a\n---\n",
        ],
    )
    def test_text_that_does_not_open_and_close_a_block_is_bad_frontmatter(self, text):
        with pytest.raises(InvalidPackageError) as caught:
            split_frontmatter(text)

        assert caught.value.reason == "BAD_FRONTMATTER"

    def test_the_messages_name_the_entrypoint_that_was_given(self):
        with pytest.raises(InvalidPackageError) as unopened:
            split_frontmatter("nothing", "INDEX.md")
        with pytest.raises(InvalidPackageError) as unclosed:
            split_frontmatter("---\nname: a\n", "INDEX.md")

        assert unopened.value.message.startswith("INDEX.md must start with a YAML block")
        assert unclosed.value.message == "The frontmatter of INDEX.md is not closed by a '---' line"

    def test_without_a_name_the_messages_cover_both_entrypoints(self):
        with pytest.raises(InvalidPackageError) as caught:
            split_frontmatter("---\nname: a\n")

        assert "SKILL.md" in caught.value.message and "INDEX.md" in caught.value.message

    @pytest.mark.parametrize(("size", "accepted"), [(16 * KIB, True), (16 * KIB + 1, False)])
    def test_the_block_is_limited_in_bytes(self, size, accepted):
        block = "#" * (size - 1) + "\n"
        text = f"---\n{block}---\nbody"

        if accepted:
            assert split_frontmatter(text) == (block, "body")
        else:
            with pytest.raises(InvalidPackageError, match="exceeds the limit"):
                split_frontmatter(text)

    def test_the_limit_counts_bytes_not_characters(self):
        fits = "\u00e9" * (8 * KIB - 1) + "\n"
        over = "\u00e9" * (8 * KIB) + "\n"

        assert split_frontmatter(f"---\n{fits}---\n")[0] == fits
        with pytest.raises(InvalidPackageError, match="exceeds the limit"):
            split_frontmatter(f"---\n{over}---\n")

    def test_a_lone_surrogate_is_measured_instead_of_crashing(self):
        text = "---\nname: \ud800\n---\nbody"

        assert split_frontmatter(text) == ("name: \ud800\n", "body")

    def test_the_inspector_splits_the_entrypoint_with_this_function(self, monkeypatch):
        calls = []
        real = inspector.split_frontmatter

        def spy(text, entrypoint=None):
            calls.append((text, entrypoint))
            return real(text, entrypoint) if entrypoint else real(text)

        monkeypatch.setattr(inspector, "split_frontmatter", spy)
        text = frontmatter_doc(VALID_BLOCK, "# Body\n")

        parsed = inspect_text(text)

        assert calls == [(text, "SKILL.md")]
        assert parsed.search_text == "# Body"

    def test_the_inspector_and_the_function_agree_on_the_body(self):
        for body in ("", "plain\n", "---\nnot: front\n---\n", "\u20ac" * 50):
            text = frontmatter_doc(VALID_BLOCK, body)

            parsed = inspect_text(text)

            assert parsed.search_text == split_frontmatter(text)[1].strip()

    def test_a_second_byte_order_mark_is_rejected_by_both(self):
        text = chr(0xFEFF) + frontmatter_doc(VALID_BLOCK, "x", bom=True)
        data = make_zip({"SKILL.md": text.encode("utf-8")})

        assert reason_of(data) == "BAD_FRONTMATTER"
        with pytest.raises(InvalidPackageError):
            split_frontmatter(text[1:])


class TestYamlAnchors:
    def test_anchor_and_alias_pair(self):
        error = block_rejection(VALID_BLOCK + "a: &x value\nb: *x\n")
        assert error.reason == "BAD_FRONTMATTER"
        assert "anchor" in error.message.lower()

    def test_an_anchor_without_any_alias_is_still_rejected(self):
        assert block_rejection(VALID_BLOCK + "a: &lonely value\n").reason == "BAD_FRONTMATTER"

    def test_anchors_on_collections(self):
        assert block_rejection(VALID_BLOCK + "a: &m {k: v}\n").reason == "BAD_FRONTMATTER"
        assert block_rejection(VALID_BLOCK + "a: &s [1, 2]\nb: *s\n").reason == "BAD_FRONTMATTER"

    def test_merge_keys_need_aliases_and_are_rejected(self):
        block = VALID_BLOCK + "base: &base {k: v}\nderived:\n  <<: *base\n  extra: 1\n"
        assert block_rejection(block).reason == "BAD_FRONTMATTER"

    def test_an_alias_to_an_undefined_anchor_is_rejected(self):
        assert block_rejection(VALID_BLOCK + "a: *missing\n").reason == "BAD_FRONTMATTER"

    def test_anchors_in_the_name_description_and_version_are_rejected(self):
        for block in (
            "name: &n sample\ndescription: d\nversion: 1.0.0\n",
            "name: sample\ndescription: &d d\nversion: 1.0.0\n",
            "name: sample\ndescription: d\nversion: &v 1.0.0\n",
        ):
            assert block_rejection(block).reason == "BAD_FRONTMATTER"

    def test_the_billion_laughs_attack_is_rejected_before_any_expansion(self):
        lines = ['a: &a ["lol","lol","lol","lol","lol","lol","lol","lol","lol"]']
        previous = "a"
        for current in "bcdefghi":
            references = ",".join([f"*{previous}"] * 9)
            lines.append(f"{current}: &{current} [{references}]")
            previous = current
        block = VALID_BLOCK + "\n".join(lines) + "\n"
        tracemalloc.start()
        started = time.perf_counter()
        try:
            error = block_rejection(block)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert error.reason == "BAD_FRONTMATTER"
        assert time.perf_counter() - started < 1
        assert peak < 8 * MIB

    def test_anchor_look_alikes_inside_scalars_are_fine(self):
        block = (
            "name: sample\nversion: 1.0.0\n"
            'description: "Research & development: uses *stars* and &ampersands"\n'
            "plain: R&D uses a*b and c&d\n"
            "quoted: '*not an alias'\n"
        )
        parsed = inspect_block(block)
        assert parsed.description == "Research & development: uses *stars* and &ampersands"
        assert parsed.metadata["plain"] == "R&D uses a*b and c&d"
        assert parsed.metadata["quoted"] == "*not an alias"


class TestDuplicateKeys:
    def test_duplicate_top_level_keys_are_rejected(self):
        error = block_rejection("name: first\nname: second\ndescription: d\nversion: 1.0.0\n")
        assert error.reason == "BAD_FRONTMATTER"
        assert "Duplicate" in error.message

    def test_duplicates_of_optional_keys_are_rejected(self):
        assert block_rejection(VALID_BLOCK + "tags: [a]\ntags: [b]\n").reason == "BAD_FRONTMATTER"
        assert block_rejection(VALID_BLOCK + "x: 1\nx: 2\n").reason == "BAD_FRONTMATTER"

    def test_duplicate_keys_in_nested_mappings_are_rejected(self):
        assert block_rejection(VALID_BLOCK + "outer:\n  a: 1\n  a: 2\n").reason == "BAD_FRONTMATTER"
        assert block_rejection(VALID_BLOCK + "outer: {a: 1, a: 2}\n").reason == "BAD_FRONTMATTER"

    def test_quoting_does_not_hide_a_duplicate(self):
        assert block_rejection(VALID_BLOCK + "'name': other\n").reason == "BAD_FRONTMATTER"

    def test_the_same_key_in_different_mappings_is_fine(self):
        block = VALID_BLOCK + "first: {k: 1}\nsecond: {k: 2}\nlist:\n  - k: 1\n  - k: 2\n"
        parsed = inspect_block(block)
        assert parsed.metadata["first"] == {"k": 1}
        assert parsed.metadata["list"] == [{"k": 1}, {"k": 2}]

    def test_a_key_used_as_a_value_is_not_a_duplicate(self):
        parsed = inspect_block(VALID_BLOCK + "mirror: name\nlist: [name, name]\n")
        assert parsed.metadata["mirror"] == "name"


class TestEntrypointEncoding:
    def test_non_utf8_entrypoints_are_rejected(self):
        for encoded in (
            b"---\nname: a\nversion: 1.0.0\ndescription: caf\xe9\n---\nbody",
            "---\nname: a\n---\n".encode("utf-16"),
            "---\nname: a\n---\n".encode("utf-16-le"),
            b"---\nname: a\nversion: 1.0.0\ndescription: d\n---\n\xff\xfe broken body",
            b"\x80\x81\x82",
        ):
            error = rejection(make_zip({"SKILL.md": encoded}), version="1.0.0")
            assert error.reason == "BAD_FRONTMATTER"
            assert "UTF-8" in error.message

    def test_invalid_utf8_far_into_the_body_is_still_rejected(self):
        body = b"x" * (600 * KIB) + b"\xff" + b"y" * 10
        data = make_zip({"SKILL.md": frontmatter_doc(VALID_BLOCK).encode("utf-8") + body})
        assert reason_of(data) == "BAD_FRONTMATTER"

    def test_nul_bytes_anywhere_in_the_entrypoint_are_rejected(self):
        head = frontmatter_doc(VALID_BLOCK).encode("utf-8")
        assert reason_of(make_zip({"SKILL.md": head + b"abc\x00def"})) == "BAD_FRONTMATTER"
        far = head + b"x" * (600 * KIB) + b"\x00"
        assert reason_of(make_zip({"SKILL.md": far})) == "BAD_FRONTMATTER"

    def test_a_truncated_multibyte_sequence_at_the_end_is_rejected(self):
        data = make_zip({"SKILL.md": frontmatter_doc(VALID_BLOCK).encode("utf-8") + b"abc\xe2\x82"})
        assert reason_of(data) == "BAD_FRONTMATTER"

    def test_multibyte_text_everywhere_is_accepted(self):
        body = (EURO + E_ACUTE + chr(0x1F600)) * 100_000
        parsed = inspect_block(VALID_BLOCK, body)
        assert parsed.search_text == body[:20_000]

    def test_a_large_entrypoint_is_processed_with_bounded_memory(self):
        body = ("lorem ipsum dolor sit amet\n" * 200_000).encode("utf-8")
        data = make_zip({"SKILL.md": frontmatter_doc(VALID_BLOCK).encode("utf-8") + body})
        tracemalloc.start()
        try:
            parsed = inspect_package(data)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert len(parsed.search_text) == 20_000
        assert parsed.manifest[0].size > 5 * MIB
        assert peak < 6 * MIB


class TestNameField:
    @pytest.mark.parametrize(
        "name",
        [
            "a",
            "0",
            "9lives",
            "123abc",
            "web-search",
            "v2-tools",
            "a-b-c-1",
            "a" * 64,
            "x" * 63 + "9",
        ],
    )
    def test_valid_names(self, name):
        assert inspect_package(make_skill_zip(name, "1.0.0")).name == name

    @pytest.mark.parametrize(
        "name",
        [
            "Web-Search",
            "UPPER",
            "web_search",
            "-web",
            "web-",
            "-",
            "web--search",
            "web search",
            "web.search",
            "web/search",
            "caf" + E_ACUTE,
            "",
            "a" * 65,
            "a" * 200,
            "123e4567-e89b-12d3-a456-426614174000",
            "123e4567e89b12d3a456426614174000",
            "abcdef01-23456789-abcdef01-23456789",
            "00000000-0000-0000-0000-000000000000",
            123,
            1.5,
            True,
            ["a"],
            {"a": "b"},
        ],
    )
    def test_invalid_names(self, name):
        error = rejection(make_skill_zip(name, "1.0.0"))
        assert error.reason == "INVALID_FIELD"
        assert "name" in error.message.lower()

    def test_a_missing_name(self):
        assert block_rejection("description: d\nversion: 1.0.0\n").reason == "MISSING_FIELD"

    def test_a_null_name_counts_as_missing(self):
        error = block_rejection("name:\ndescription: d\nversion: 1.0.0\n")
        assert error.reason == "MISSING_FIELD"
        assert "name" in error.message

    def test_names_never_look_like_uuids(self):
        sample = [
            "123e4567-e89b-12d3-a456-426614174000",
            "123e4567e89b12d3a456426614174000",
            "abcdef01-23456789-abcdef01-23456789",
        ]
        assert all(looks_like_uuid(value) for value in sample)
        assert not looks_like_uuid("web-search")
        assert not looks_like_uuid("123e4567-e89b-12d3-a456-42661417400")
        assert not looks_like_uuid("")
        assert not looks_like_uuid(None)
        assert not looks_like_uuid(123)

    def test_a_name_with_hex_digits_but_the_wrong_length_is_a_valid_name(self):
        assert inspect_package(make_skill_zip("deadbeef-cafe", "1.0.0")).name == "deadbeef-cafe"


class TestDescriptionField:
    def test_description_is_stripped(self):
        parsed = inspect_package(make_skill_zip(description="   padded text \n"))
        assert parsed.description == "padded text"

    def test_multiline_descriptions_keep_internal_newlines(self):
        block = "name: sample\nversion: 1.0.0\ndescription: |\n  first line\n  second line\n"
        assert inspect_block(block).description == "first line\nsecond line"

    def test_folded_descriptions_are_joined(self):
        block = "name: sample\nversion: 1.0.0\ndescription: >\n  first line\n  second line\n"
        assert inspect_block(block).description == "first line second line"

    @pytest.mark.parametrize("length", [1, 2, 1024])
    def test_valid_lengths(self, length):
        assert len(inspect_package(make_skill_zip(description="d" * length)).description) == length

    def test_the_limit_applies_after_stripping(self):
        parsed = inspect_package(make_skill_zip(description="  " + "d" * 1024 + "  "))
        assert len(parsed.description) == 1024

    @pytest.mark.parametrize(
        "description", ["", "   ", "\n\t ", "d" * 1025, 123, ["a"], {"a": 1}, True, 1.5]
    )
    def test_invalid_descriptions(self, description):
        error = rejection(make_skill_zip(description=description))
        assert error.reason == "INVALID_FIELD"
        assert "description" in error.message.lower()

    def test_a_missing_description(self):
        error = block_rejection("name: sample\nversion: 1.0.0\n")
        assert error.reason == "MISSING_FIELD"
        assert "description" in error.message

    def test_a_null_description_counts_as_missing(self):
        assert (
            block_rejection("name: sample\nversion: 1.0.0\ndescription:\n").reason
            == "MISSING_FIELD"
        )


class TestVersionField:
    def test_version_from_the_frontmatter(self):
        assert inspect_package(make_skill_zip("v", "2.5.1-rc.1")).version == "2.5.1-rc.1"

    def test_version_from_the_upload_argument(self):
        data = make_skill_zip("v", None)
        assert inspect_package(data, version="3.0.0").version == "3.0.0"

    def test_both_sources_may_agree(self):
        data = make_skill_zip("v", "1.2.3")
        assert inspect_package(data, version="1.2.3").version == "1.2.3"

    def test_disagreeing_sources_are_a_version_mismatch(self):
        error = rejection(make_skill_zip("v", "1.2.3"), version="1.2.4")
        assert error.reason == "VERSION_MISMATCH"
        assert "1.2.3" in error.message
        assert "1.2.4" in error.message

    def test_a_prerelease_never_matches_its_release(self):
        assert reason_of(make_skill_zip("v", "1.0.0"), version="1.0.0-rc.1") == "VERSION_MISMATCH"
        assert reason_of(make_skill_zip("v", "1.0.0-rc.1"), version="1.0.0") == "VERSION_MISMATCH"

    def test_a_version_from_nowhere_is_a_missing_field(self):
        error = rejection(make_skill_zip("v", None))
        assert error.reason == "MISSING_FIELD"
        assert "version" in error.message

    def test_a_null_frontmatter_version_counts_as_absent(self):
        data = make_zip({"SKILL.md": frontmatter_doc("name: v\ndescription: d\nversion:\n")})
        assert reason_of(data) == "MISSING_FIELD"
        assert inspect_package(data, version="1.0.0").version == "1.0.0"

    @pytest.mark.parametrize(
        ("literal", "hint"),
        [
            ("1.0", True),
            ("1", True),
            ("2.5", True),
            ("-1", True),
            ("true", False),
            ("[1, 0, 0]", False),
            ("{a: 1}", False),
        ],
    )
    def test_non_string_yaml_versions_are_invalid_fields(self, literal, hint):
        error = block_rejection(f"name: v\ndescription: d\nversion: {literal}\n", version=None)
        assert error.reason == "INVALID_FIELD"
        assert ("quote" in error.message.lower()) is hint

    def test_a_quoted_number_is_judged_as_a_semver_string(self):
        error = block_rejection('name: v\ndescription: d\nversion: "1.0"\n', version=None)
        assert error.reason == "INVALID_FIELD"
        assert "MAJOR.MINOR.PATCH" in error.message

    def test_an_unquoted_dotted_triple_is_a_string_and_is_accepted(self):
        block = "name: v\ndescription: d\nversion: 1.0.0\n"
        assert inspect_block(block, version=None).version == "1.0.0"

    @pytest.mark.parametrize(
        "version",
        [
            "v1.0.0",
            "1.0",
            "1.0.0.0",
            "01.0.0",
            "1.0.0+build",
            "1.0.0-",
            "latest",
            "",
            "1.0.0 ",
            "x" * 51,
            "1.0.0-" + "a" * 45,
        ],
    )
    def test_invalid_semver_in_the_frontmatter(self, version):
        error = rejection(make_skill_zip("v", version))
        assert error.reason == "INVALID_FIELD"
        assert "version" in error.message.lower()

    def test_build_metadata_in_the_frontmatter_is_explained(self):
        error = rejection(make_skill_zip("v", "1.0.0+exp.sha.5114f85"))
        assert error.reason == "INVALID_FIELD"
        assert "build metadata" in error.message

    def test_a_valid_frontmatter_version_is_still_checked_against_a_bad_argument(self):
        with pytest.raises(InvalidVersionError):
            inspect_package(make_skill_zip("v", "1.0.0"), version="not-semver")

    def test_an_invalid_argument_is_a_validation_error_not_a_package_error(self):
        with pytest.raises(ValidationError) as caught:
            inspect_package(make_skill_zip("v", None), version="1.0")
        assert not isinstance(caught.value, InvalidPackageError)
        assert caught.value.code == "VALIDATION_ERROR"

    def test_the_argument_is_validated_before_the_archive_is_opened(self):
        with pytest.raises(InvalidVersionError):
            inspect_package(b"not even a zip", version="bad")

    def test_an_invalid_frontmatter_version_is_reported_even_with_a_valid_argument(self):
        error = rejection(make_skill_zip("v", "banana"), version="1.0.0")
        assert error.reason == "INVALID_FIELD"

    @pytest.mark.parametrize(
        "version", ["0.0.0", "1.0.0-alpha.1", "10.20.30", "1.0.0-0.3.7", "1.0.0-x-y"]
    )
    def test_assorted_valid_versions(self, version):
        assert inspect_package(make_skill_zip("v", version)).version == version
        assert inspect_package(make_skill_zip("v", None), version=version).version == version


class TestDisplayNameField:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("web-search", "Web Search"),
            ("a", "A"),
            ("code-review-helper", "Code Review Helper"),
            ("x86-asm", "X86 Asm"),
            ("single", "Single"),
            ("v2-tools", "V2 Tools"),
        ],
    )
    def test_default_is_the_name_in_title_case(self, name, expected):
        assert inspect_package(make_skill_zip(name, "1.0.0")).display_name == expected

    def test_explicit_display_name(self):
        parsed = inspect_package(make_skill_zip(display_name="  My Fancy Skill  "))
        assert parsed.display_name == "My Fancy Skill"

    def test_unicode_display_names(self):
        parsed = inspect_package(make_skill_zip(display_name="Caf" + E_ACUTE + " " + EURO))
        assert parsed.display_name == "Caf" + E_ACUTE + " " + EURO

    def test_blank_or_null_display_names_fall_back_to_the_default(self):
        assert (
            inspect_package(make_skill_zip("web-search", display_name="   ")).display_name
            == "Web Search"
        )
        block = "name: web-search\ndescription: d\nversion: 1.0.0\ndisplay_name:\n"
        assert inspect_block(block).display_name == "Web Search"

    def test_the_length_limit(self):
        assert len(inspect_package(make_skill_zip(display_name="d" * 255)).display_name) == 255
        error = rejection(make_skill_zip(display_name="d" * 256))
        assert error.reason == "INVALID_FIELD"
        assert "display_name" in error.message

    @pytest.mark.parametrize("value", [123, ["a"], {"a": 1}, True])
    def test_non_string_display_names(self, value):
        assert reason_of(make_skill_zip(display_name=value)) == "INVALID_FIELD"


class TestTagsField:
    def test_tags_are_stripped_and_lowercased(self):
        parsed = inspect_package(
            make_skill_zip(tags=["Python", " web ", "C++", "node.js", "snake_case", "a-b"])
        )
        assert parsed.tags == ["python", "web", "c++", "node.js", "snake_case", "a-b"]

    def test_duplicates_are_removed_keeping_the_first_occurrence_order(self):
        parsed = inspect_package(make_skill_zip(tags=["b", "a", "B", " a ", "c", "b"]))
        assert parsed.tags == ["b", "a", "c"]

    def test_no_tags(self):
        assert inspect_package(make_skill_zip()).tags == []
        assert inspect_package(make_skill_zip(tags=[])).tags == []
        assert inspect_block(VALID_BLOCK + "tags:\n").tags == []

    def test_thirty_two_characters_is_the_longest_tag(self):
        assert inspect_package(make_skill_zip(tags=["t" * 32])).tags == ["t" * 32]
        assert reason_of(make_skill_zip(tags=["t" * 33])) == "INVALID_FIELD"

    def test_twenty_tags_are_allowed_and_twenty_one_are_not(self):
        twenty = [f"tag{index}" for index in range(20)]
        assert inspect_package(make_skill_zip(tags=twenty)).tags == twenty
        error = rejection(make_skill_zip(tags=[*twenty, "tag20"]))
        assert error.reason == "INVALID_FIELD"
        assert "20" in error.message

    def test_the_count_is_taken_after_removing_duplicates(self):
        tags = [f"tag{index % 20}" for index in range(30)]
        assert len(inspect_package(make_skill_zip(tags=tags)).tags) == 20

    @pytest.mark.parametrize(
        "tag",
        [
            "has space",
            "-lead",
            ".lead",
            "_lead",
            "+lead",
            "",
            "   ",
            "t" * 33,
            E_ACUTE,
            E_ACUTE.upper(),
            "emoji" + chr(0x1F600),
            "slash/tag",
            "colon:tag",
            "tab\there",
            "new\nline",
            "semi;colon",
        ],
    )
    def test_invalid_tag_values(self, tag):
        error = rejection(make_skill_zip(tags=[tag]))
        assert error.reason == "INVALID_FIELD"
        assert "tag" in error.message.lower()

    @pytest.mark.parametrize("value", [1, True, None, ["nested"], {"a": 1}, 1.5])
    def test_tags_must_be_strings(self, value):
        assert reason_of(make_skill_zip(tags=["ok", value])) == "INVALID_FIELD"

    @pytest.mark.parametrize("value", ["a, b", "single", 5, True, {"a": 1}])
    def test_tags_must_be_a_list(self, value):
        block = VALID_BLOCK + f"tags: {json.dumps(value)}\n"
        assert block_rejection(block).reason == "INVALID_FIELD"


class TestCategoryField:
    def test_category_is_stripped_and_lowercased(self):
        assert (
            inspect_package(make_skill_zip(category="  Data-Science ")).category == "data-science"
        )

    def test_no_category(self):
        assert inspect_package(make_skill_zip()).category is None
        assert inspect_block(VALID_BLOCK + "category:\n").category is None

    def test_blank_category_counts_as_none(self):
        assert inspect_package(make_skill_zip(category="   ")).category is None

    @pytest.mark.parametrize("category", ["a", "9x", "x" * 64, "data-science", "a-b-c"])
    def test_valid_categories(self, category):
        assert inspect_package(make_skill_zip(category=category)).category == category

    @pytest.mark.parametrize(
        "category",
        ["has space", "under_score", "-lead", "x" * 65, E_ACUTE, "dot.ted", "slash/ed", "a+b"],
    )
    def test_invalid_categories(self, category):
        error = rejection(make_skill_zip(category=category))
        assert error.reason == "INVALID_FIELD"
        assert "category" in error.message.lower()

    @pytest.mark.parametrize("value", [5, True, ["a"], {"a": 1}])
    def test_category_must_be_a_string(self, value):
        assert reason_of(make_skill_zip(category=value)) == "INVALID_FIELD"


class TestKindField:
    def test_matching_kinds_are_accepted(self):
        assert inspect_package(make_skill_zip(kind="skill")).kind == "skill"
        assert inspect_package(make_grounding_zip(kind="grounding")).kind == "grounding"

    def test_kind_is_not_stored_in_the_metadata(self):
        assert inspect_package(make_skill_zip(kind="skill")).metadata == {}

    def test_kind_must_match_the_entrypoint(self):
        error = rejection(make_skill_zip(kind="grounding"))
        assert error.reason == "KIND_MISMATCH"
        assert "grounding" in error.message
        assert reason_of(make_grounding_zip(kind="skill")) == "KIND_MISMATCH"

    @pytest.mark.parametrize(
        "value", ["Skill", "GROUNDING", "other", "", " skill", 5, ["skill"], True]
    )
    def test_invalid_kind_values(self, value):
        assert reason_of(make_skill_zip(kind=value)) == "INVALID_FIELD"

    def test_a_null_kind_is_ignored(self):
        assert inspect_block(VALID_BLOCK + "kind:\n").kind == "skill"


class TestMetadata:
    def test_unknown_fields_are_kept_in_order(self):
        parsed = inspect_package(
            make_skill_zip(
                extra_frontmatter={"license": "MIT", "author": "Ana", "homepage": "https://x.org"}
            )
        )
        assert parsed.metadata == {"license": "MIT", "author": "Ana", "homepage": "https://x.org"}
        assert list(parsed.metadata) == ["license", "author", "homepage"]

    def test_known_fields_never_leak_into_the_metadata(self):
        parsed = inspect_package(
            make_skill_zip(
                tags=["a"], category="c", display_name="D", kind="skill", extra_frontmatter={"k": 1}
            )
        )
        assert parsed.metadata == {"k": 1}

    def test_a_field_called_metadata_is_an_ordinary_extra_field(self):
        parsed = inspect_package(make_skill_zip(extra_frontmatter={"metadata": {"inner": True}}))
        assert parsed.metadata == {"metadata": {"inner": True}}

    def test_structured_values(self):
        block = (
            VALID_BLOCK
            + "author:\n  name: Ana\n  emails: [a@x.org, b@x.org]\n"
            + "ratio: 0.5\ncount: 3\nenabled: true\ndisabled: false\nnothing: null\ntext: hello\n"
        )
        assert inspect_block(block).metadata == {
            "author": {"name": "Ana", "emails": ["a@x.org", "b@x.org"]},
            "ratio": 0.5,
            "count": 3,
            "enabled": True,
            "disabled": False,
            "nothing": None,
            "text": "hello",
        }

    def test_dates_and_datetimes_become_iso_8601(self):
        block = (
            VALID_BLOCK
            + "released: 2024-05-17\nupdated: 2024-05-17 10:30:00\n"
            + "zoned: 2024-05-17T10:30:00+02:00\nutc: 2024-05-17T10:30:00Z\n"
            + "nested:\n  day: 2024-01-02\n  days: [2024-01-03, 2024-01-04]\n"
        )
        metadata = inspect_block(block).metadata
        assert metadata["released"] == "2024-05-17"
        assert metadata["updated"] == "2024-05-17T10:30:00"
        assert metadata["zoned"] == "2024-05-17T10:30:00+02:00"
        assert metadata["utc"].startswith("2024-05-17T10:30:00")
        assert metadata["nested"] == {"day": "2024-01-02", "days": ["2024-01-03", "2024-01-04"]}

    def test_date_keys_in_nested_mappings_become_iso_strings(self):
        metadata = inspect_block(
            VALID_BLOCK + "history:\n  2024-05-17: first\n  2024-06-01: second\n"
        ).metadata
        assert metadata["history"] == {"2024-05-17": "first", "2024-06-01": "second"}

    def test_non_string_nested_keys_become_strings(self):
        metadata = inspect_block(
            VALID_BLOCK + "table:\n  1: one\n  2.5: two\n  null: three\n"
        ).metadata
        assert metadata["table"] == {"1": "one", "2.5": "two", "None": "three"}

    def test_types_without_a_json_form_are_converted_with_str(self):
        block = (
            VALID_BLOCK
            + "blob: !!binary aGVsbG8=\nunique: !!set {a, b}\nordered: !!omap [{a: 1}, {b: 2}]\n"
        )
        metadata = inspect_block(block).metadata
        assert metadata["blob"] == str(b"hello")
        assert (
            metadata["unique"].startswith("{")
            and "'a'" in metadata["unique"]
            and "'b'" in metadata["unique"]
        )
        assert metadata["ordered"] == [["a", 1], ["b", 2]]

    def test_non_finite_floats_become_strings(self):
        metadata = inspect_block(VALID_BLOCK + "nan: .nan\npos: .inf\nneg: -.inf\n").metadata
        assert metadata == {"nan": "nan", "pos": "inf", "neg": "-inf"}

    def test_metadata_is_always_strictly_json_serializable(self):
        block = (
            VALID_BLOCK
            + "d: 2024-01-01\nn: .nan\nb: !!binary aGk=\ns: !!set {x}\nk:\n  2024-01-01: v\n  3: w\n"
        )
        metadata = inspect_block(block).metadata
        encoded = json.dumps(metadata, allow_nan=False)
        assert json.loads(encoded) == metadata

    def test_large_metadata_values_within_the_frontmatter_limit(self):
        value = "v" * 12_000
        assert inspect_block(VALID_BLOCK + f"big: {value}\n").metadata == {"big": value}


class TestSearchText:
    def test_search_text_is_the_body_without_the_frontmatter(self):
        parsed = inspect_block(VALID_BLOCK, "# Title\n\nSome searchable words.\n")
        assert parsed.search_text == "# Title\n\nSome searchable words."
        assert "Sample description" not in parsed.search_text
        assert "name:" not in parsed.search_text

    def test_surrounding_whitespace_is_trimmed(self):
        assert inspect_block(VALID_BLOCK, "\n\n   text   \n\n").search_text == "text"

    def test_an_empty_body_gives_empty_search_text(self):
        assert inspect_block(VALID_BLOCK, "").search_text == ""
        assert inspect_block(VALID_BLOCK, "\n\n").search_text == ""

    def test_search_text_is_cut_at_twenty_thousand_characters(self):
        body = "".join(chr(97 + index % 26) for index in range(30_000))
        parsed = inspect_block(VALID_BLOCK, body)
        assert len(parsed.search_text) == 20_000
        assert parsed.search_text == body[:20_000]

    def test_the_cut_counts_characters_not_bytes(self):
        parsed = inspect_block(VALID_BLOCK, EURO * 25_000)
        assert parsed.search_text == EURO * 20_000

    def test_a_body_shorter_than_the_limit_is_kept_whole(self):
        body = "x" * 19_999
        assert inspect_block(VALID_BLOCK, body).search_text == body

    def test_control_characters_are_removed_from_the_search_text(self):
        data = make_zip(
            {
                "SKILL.md": frontmatter_doc(VALID_BLOCK).encode("utf-8")
                + b"clean\x1b[31mred\x07 bell\x0cform\x7f"
            }
        )
        data_without_nul = inspect_package(data)
        assert "\x1b" not in data_without_nul.search_text
        assert "\x07" not in data_without_nul.search_text
        assert "\x7f" not in data_without_nul.search_text
        assert "clean[31mred bellform" == data_without_nul.search_text

    def test_tabs_newlines_and_carriage_returns_survive(self):
        assert inspect_block(VALID_BLOCK, "a\tb\r\nc\nd").search_text == "a\tb\r\nc\nd"

    def test_the_grounding_index_body_is_indexed_too(self):
        parsed = inspect_package(make_grounding_zip(body="Reference tables about weather.\n"))
        assert parsed.search_text == "Reference tables about weather."

    def test_other_files_never_feed_the_search_text(self):
        parsed = inspect_package(
            make_skill_zip(body="entry body", files={"docs/a.md": "secret words"})
        )
        assert parsed.search_text == "entry body"


class TestTextSampleAndMediaTypes:
    def test_plain_ascii_and_utf8_are_text(self):
        assert is_text_sample(b"hello world\n") is True
        assert is_text_sample((EURO + E_ACUTE).encode("utf-8")) is True
        assert is_text_sample(b"") is True

    def test_nul_bytes_make_a_sample_binary(self):
        assert is_text_sample(b"abc\x00def") is False
        assert is_text_sample(b"\x00") is False

    def test_invalid_utf8_makes_a_sample_binary(self):
        assert is_text_sample(b"caf\xe9 au lait") is False
        assert is_text_sample(b"\xff\xfe") is False
        assert is_text_sample(b"\xc0\xaf") is False
        assert is_text_sample(b"\xed\xa0\x80") is False
        assert is_text_sample(b"ab\xe2\x28\xa1") is False

    def test_a_multibyte_character_cut_at_the_end_of_the_sample_is_tolerated(self):
        encoded = EURO.encode("utf-8")
        assert len(encoded) == 3
        assert is_text_sample(b"abc" + encoded[:1]) is True
        assert is_text_sample(b"abc" + encoded[:2]) is True
        assert is_text_sample(b"abc" + chr(0x1F600).encode("utf-8")[:3]) is True

    def test_a_cut_character_is_not_tolerated_when_the_sample_is_the_whole_file(self):
        assert is_text_sample(b"abc" + EURO.encode("utf-8")[:2], complete=True) is False
        assert is_text_sample(b"abc" + EURO.encode("utf-8"), complete=True) is True

    def test_a_cut_followed_by_garbage_is_still_binary(self):
        assert is_text_sample(b"abc" + EURO.encode("utf-8")[:2] + b"x") is False

    def test_a_character_cut_at_the_sample_boundary_still_counts_as_text_in_a_manifest(self):
        content = b"a" * 8191 + EURO.encode("utf-8") + b"tail"
        manifest = manifest_of(inspect_package(make_skill_zip(files={"cut.txt": content})))
        assert manifest["cut.txt"].text is True

    def test_only_the_first_eight_kibibytes_are_examined(self):
        late_nul = manifest_of(
            inspect_package(make_skill_zip(files={"a.txt": b"a" * 8192 + b"\x00"}))
        )
        late_bad = manifest_of(
            inspect_package(make_skill_zip(files={"a.txt": b"a" * 8192 + b"\xff\xfe"}))
        )
        early_nul = manifest_of(
            inspect_package(make_skill_zip(files={"a.txt": b"a" * 8191 + b"\x00"}))
        )
        early_bad = manifest_of(
            inspect_package(make_skill_zip(files={"a.txt": b"a" * 8191 + b"\xff"}))
        )
        assert late_nul["a.txt"].text is True
        assert late_bad["a.txt"].text is True
        assert early_nul["a.txt"].text is False
        assert early_bad["a.txt"].text is False

    def test_a_small_file_ending_in_a_broken_character_is_binary(self):
        manifest = manifest_of(
            inspect_package(make_skill_zip(files={"a.txt": b"abc" + EURO.encode("utf-8")[:2]}))
        )
        assert manifest["a.txt"].text is False

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("run.py", "text/x-python"),
            ("scripts/run.py", "text/x-python"),
            ("data.json", "application/json"),
            ("index.html", "text/html"),
            ("logo.png", "image/png"),
            ("photo.jpg", "image/jpeg"),
            ("notes.txt", "text/plain"),
            ("table.csv", "text/csv"),
            ("README.md", "text/markdown"),
            ("config.yaml", "application/yaml"),
            ("config.yml", "application/yaml"),
            ("pyproject.toml", "application/toml"),
            ("query.sql", "application/sql"),
            ("store.sqlite", "application/vnd.sqlite3"),
            ("store.db", "application/vnd.sqlite3"),
            ("archive.zip", "application/zip"),
            ("README", "application/octet-stream"),
            ("data.unknownext", "application/octet-stream"),
            ("noext.", "application/octet-stream"),
            (".hidden", "application/octet-stream"),
        ],
    )
    def test_media_type_guessing(self, path, expected):
        assert guess_media_type(path) == expected

    def test_media_type_guessing_is_case_insensitive_on_extensions(self):
        assert guess_media_type("SCRIPT.PY") == "text/x-python"
        assert guess_media_type("Photo.PNG") == "image/png"


class TestPathValidation:
    @pytest.mark.parametrize(
        "path",
        [
            "SKILL.md",
            "scripts/run.py",
            "a/b/c/d/e.txt",
            ".hidden",
            "dir/.hidden/file",
            "with space/file name.txt",
            "caf" + E_ACUTE + "/" + EURO + ".md",
            "a..b",
            "..a",
            "a..",
            "file.",
            "x" * 255,
            "colon:name.txt",
            "a/b:c",
        ],
    )
    def test_safe_paths_are_returned_unchanged(self, path):
        assert validate_requested_path(path) == path

    @pytest.mark.parametrize(
        "path",
        [
            "",
            "..",
            ".",
            "../etc/passwd",
            "a/../../etc/passwd",
            "a/..",
            "a/../b",
            "/etc/passwd",
            "/",
            "//server/share",
            "a//b",
            "a/",
            "./a",
            "a/./b",
            "a\\b",
            "..\\..\\windows",
            "C:/Windows/system32",
            "c:evil",
            "a:b",
            "a\x00b",
            "\x00",
            "a\nb",
            "a\tb",
            "a\x1fb",
            "a\x7fb",
            "a" + chr(0x85) + "b",
            "evil" + chr(0x202E) + "txt",
            "a" + chr(0x2066) + "b",
            "x" * 256,
            "a/.../b",
            "a/ /b",
            "a/. ./b",
        ],
    )
    def test_unsafe_paths_are_rejected_with_the_invalid_path_reason(self, path):
        with pytest.raises(ValidationError) as caught:
            validate_requested_path(path)
        assert caught.value.reason == "INVALID_PATH"
        assert caught.value.code == "VALIDATION_ERROR"
        assert not isinstance(caught.value, InvalidPackageError)

    @pytest.mark.parametrize("value", [None, 5, b"a/b", ["a"]])
    def test_non_string_paths_are_rejected(self, value):
        with pytest.raises(ValidationError) as caught:
            validate_requested_path(value)
        assert caught.value.reason == "INVALID_PATH"

    def test_the_maximum_length_can_be_changed(self):
        assert validate_requested_path("x" * 300, max_length=300) == "x" * 300
        with pytest.raises(ValidationError):
            validate_requested_path("x" * 11, max_length=10)

    @pytest.mark.parametrize(
        ("path", "fragment"),
        [
            ("", "empty"),
            ("/etc/passwd", "absolute"),
            ("a\\b", "backslash"),
            ("a\x00b", "control"),
            ("C:/x", "drive"),
            ("a/../b", "segment"),
            ("x" * 256, "longer"),
        ],
    )
    def test_the_error_message_explains_the_problem(self, path, fragment):
        with pytest.raises(ValidationError) as caught:
            validate_requested_path(path)
        assert fragment in caught.value.message

    def test_the_error_message_quotes_a_truncated_path(self):
        with pytest.raises(ValidationError) as caught:
            validate_requested_path("../" + "z" * 1000)
        assert len(caught.value.message) < 300
        assert "zzzz" in caught.value.message


EVIL_PATHS = [
    "../evil.txt",
    "../../evil.txt",
    "a/../../evil.txt",
    "a/../b.txt",
    "docs/..",
    "/etc/passwd",
    "/evil.txt",
    "C:/Windows/evil.dll",
    "c:evil.txt",
    "..\\evil.txt",
    "a\\b.txt",
    "docs\\..\\..\\evil.txt",
    "a//b.txt",
    "./a.txt",
    "a/./b.txt",
    "a/.../b.txt",
    "a/ /b.txt",
    "a" + chr(7) + "b.txt",
    "a" + chr(0x7F) + "b.txt",
    "a" + chr(0x85) + "b.txt",
    "a" + chr(0x1B) + "[31mred.txt",
    "report" + chr(0x202E) + "txt.exe",
    "a" + chr(0x2067) + "b.txt",
    "x" * 256,
    "d/" + "y" * 256,
    "a\x00b.txt",
    "ok.txt\x00../../evil",
    "\x00",
]


class TestZipSlip:
    @pytest.mark.parametrize("evil_path", EVIL_PATHS)
    def test_unsafe_entry_paths_are_rejected(self, evil_path):
        error = rejection(make_traversal_zip(evil_path))
        assert error.reason == "UNSAFE_PATH"

    @pytest.mark.parametrize("evil_path", EVIL_PATHS)
    def test_unsafe_entry_paths_are_rejected_in_grounding_packages(self, evil_path):
        assert reason_of(make_traversal_zip(evil_path, grounding=True)) == "UNSAFE_PATH"

    @pytest.mark.parametrize("evil_path", ["../evil", "/abs", "a\\b", "a//b", "a\x00b"])
    def test_unsafe_directory_entries_are_rejected_too(self, evil_path):
        data = make_raw_package([stored_entry(evil_path + "/", external_attr=UNIX_DIRECTORY)])
        assert reason_of(data) == "UNSAFE_PATH"

    def test_traversal_hidden_inside_a_wrapper_is_rejected(self):
        data = build_raw_zip(
            [
                stored_entry("pkg/SKILL.md", skill_md("slip", "1.0.0")),
                stored_entry("pkg/../../evil.txt", "owned"),
            ]
        )
        assert reason_of(data) == "UNSAFE_PATH"

    def test_a_traversal_name_is_rejected_even_when_it_is_the_entrypoint_directory(self):
        data = build_raw_zip([stored_entry("../SKILL.md", skill_md("slip", "1.0.0"))])
        assert reason_of(data) == "UNSAFE_PATH"

    def test_nothing_is_written_to_disk(self, tmp_path, monkeypatch):
        workdir = tmp_path / "work"
        workdir.mkdir()
        monkeypatch.chdir(workdir)
        for evil_path in ("../evil.txt", "/tmp/evil.txt", "a/../../evil.txt"):
            reason_of(make_traversal_zip(evil_path))
        assert list(workdir.iterdir()) == []
        assert sorted(item.name for item in tmp_path.iterdir()) == ["work"]

    def test_the_error_message_does_not_echo_unbounded_input(self):
        error = rejection(make_traversal_zip("../" + "z" * 5000))
        assert error.reason == "UNSAFE_PATH"
        assert len(error.message) < 400

    @pytest.mark.parametrize(
        "safe_path",
        [
            "safe/evil.txt",
            "dot..dot.txt",
            "..hidden",
            "trailing.",
            "a b/c d.txt",
            "x" * 255,
            "d/" + "y" * 250,
            "caf" + E_ACUTE + "/" + EURO + ".txt",
            "colon:name.txt",
            ".github/workflows/ci.yml",
        ],
    )
    def test_benign_look_alikes_are_accepted(self, safe_path):
        assert safe_path in paths_of(inspect_package(make_traversal_zip(safe_path)))

    def test_the_path_length_limit_is_configurable(self):
        data = make_skill_zip(files={"a" * 30: "x"})
        assert inspect_package(data, PackageLimits(max_path_length=30)).file_count == 2
        assert reason_of(data, PackageLimits(max_path_length=29)) == "UNSAFE_PATH"

    def test_the_path_length_limit_counts_the_wrapper_directory(self):
        data = make_skill_zip(wrapper="w" * 100, files={"a" * 100: "x"})
        assert reason_of(data, PackageLimits(max_path_length=150)) == "UNSAFE_PATH"
        assert (
            inspect_package(data, PackageLimits(max_path_length=220)).root_prefix == "w" * 100 + "/"
        )

    def test_conflicting_names_from_the_unicode_path_extra_field_are_rejected(self):
        extra = unicode_path_extra("safe.txt", "../../evil.txt")
        data = build_raw_zip(
            [
                stored_entry("SKILL.md", skill_md("slip", "1.0.0")),
                stored_entry("safe.txt", "payload", extra=extra),
            ]
        )
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            info = archive.infolist()[1]
            assert info.orig_filename == "safe.txt"
            assert info.filename == "../../evil.txt"
        error = rejection(data)
        assert error.reason == "UNSAFE_PATH"
        assert "conflicting" in error.message

    def test_a_harmless_unicode_path_extra_field_that_changes_the_name_is_still_rejected(self):
        extra = unicode_path_extra("a.txt", "b.txt")
        data = build_raw_zip(
            [
                stored_entry("SKILL.md", skill_md("slip", "1.0.0")),
                stored_entry("a.txt", "x", extra=extra),
            ]
        )
        assert reason_of(data) == "UNSAFE_PATH"

    def test_nul_bytes_hide_a_traversal_suffix_from_the_standard_library(self):
        data = make_traversal_zip("innocent.txt\x00/../../evil")
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
        assert "innocent.txt" in names
        error = rejection(data)
        assert error.reason == "UNSAFE_PATH"
        assert "NUL" in error.message


class TestSymlinks:
    def test_symlink_to_an_absolute_target_is_rejected(self):
        error = rejection(make_symlink_zip("link", "/etc/passwd"))
        assert error.reason == "SYMLINK_NOT_ALLOWED"
        assert "link" in error.message

    def test_symlink_escaping_the_package_is_rejected(self):
        assert (
            reason_of(make_symlink_zip("docs/escape", "../../../etc/shadow"))
            == "SYMLINK_NOT_ALLOWED"
        )

    def test_symlink_inside_the_package_is_rejected_too(self):
        data = make_symlink_zip("alias.md", "SKILL.md", grounding=True)
        assert reason_of(data) == "SYMLINK_NOT_ALLOWED"

    def test_the_entrypoint_itself_cannot_be_a_symlink(self):
        data = build_raw_zip([stored_entry("SKILL.md", "/etc/passwd", external_attr=UNIX_SYMLINK)])
        assert reason_of(data) == "SYMLINK_NOT_ALLOWED"

    def test_a_symlink_directory_entry_is_rejected(self):
        data = make_raw_package([stored_entry("linkdir/", "target", external_attr=UNIX_SYMLINK)])
        assert reason_of(data) == "SYMLINK_NOT_ALLOWED"

    def test_a_symlink_inside_a_wrapper_is_rejected(self):
        data = build_raw_zip(
            [
                stored_entry("pkg/SKILL.md", skill_md("linked", "1.0.0")),
                stored_entry("pkg/link", "/etc/passwd", external_attr=UNIX_SYMLINK),
            ]
        )
        assert reason_of(data) == "SYMLINK_NOT_ALLOWED"

    def test_a_regular_file_turned_into_a_symlink_by_patching_the_attributes(self):
        data = make_skill_zip(files={"notes.txt": "plain"}, compression=zipfile.ZIP_STORED)
        assert inspect_package(data).file_count == 2
        patched = set_external_attr(data, "notes.txt", UNIX_SYMLINK)
        assert reason_of(patched) == "SYMLINK_NOT_ALLOWED"

    @pytest.mark.parametrize("mode", [0o120000, 0o120644, 0o120755, 0o120777])
    def test_every_symlink_permission_variant_is_rejected(self, mode):
        data = make_raw_package([stored_entry("l", "x", external_attr=mode << 16)])
        assert reason_of(data) == "SYMLINK_NOT_ALLOWED"

    @pytest.mark.parametrize("mode", [0o100644, 0o100755, 0o100600, 0o000000, 0o010644])
    def test_other_unix_modes_are_not_symlinks(self, mode):
        data = make_raw_package([stored_entry("f.txt", b"", external_attr=mode << 16)])
        assert "f.txt" in paths_of(inspect_package(data))

    def test_a_directory_mode_is_not_a_symlink(self):
        data = make_raw_package(
            [stored_entry("d/", b"", external_attr=0o040755 << 16), stored_entry("d/f.txt", "x")]
        )
        assert paths_of(inspect_package(data)) == ["SKILL.md", "d/f.txt"]

    def test_msdos_attributes_in_the_low_bits_are_not_mistaken_for_a_symlink(self):
        data = make_raw_package([stored_entry("f.txt", "x", external_attr=0x20)])
        assert "f.txt" in paths_of(inspect_package(data))

    def test_ordinary_executable_scripts_are_accepted(self):
        data = make_raw_package(
            [stored_entry("run.sh", "#!/bin/sh\n", external_attr=0o100755 << 16)]
        )
        assert "run.sh" in paths_of(inspect_package(data))


class TestEncryption:
    def test_encrypted_entry(self):
        error = rejection(make_encrypted_zip("secret.txt"))
        assert error.reason == "ENCRYPTED_ENTRY"
        assert "secret.txt" in error.message

    @pytest.mark.parametrize("flags", [0x1, 0x41, 0x40, 0x9, 0x801, 0x2001])
    def test_every_encryption_flag_combination_is_rejected(self, flags):
        assert reason_of(make_encrypted_zip("secret.bin", flag_bits=flags)) == "ENCRYPTED_ENTRY"

    def test_the_entrypoint_itself_cannot_be_encrypted(self):
        data = build_raw_zip([stored_entry("SKILL.md", b"\x8f" * 40, flags=0x1)])
        assert reason_of(data) == "ENCRYPTED_ENTRY"

    def test_encrypting_a_regular_archive_by_patching_the_flag_bits(self):
        data = make_skill_zip(files={"a.txt": "plain", "b.txt": "plain too"})
        assert inspect_package(data).file_count == 3
        assert reason_of(set_flag_bits(data, "b.txt", 0x1)) == "ENCRYPTED_ENTRY"

    def test_winzip_aes_entries_are_reported_as_encrypted(self):
        data = make_raw_package([stored_entry("aes.bin", b"\x01" * 30, flags=0x1, method=99)])
        assert reason_of(data) == "ENCRYPTED_ENTRY"

    def test_encrypted_directory_entries_are_rejected(self):
        data = make_raw_package([stored_entry("d/", flags=0x1, external_attr=UNIX_DIRECTORY)])
        assert reason_of(data) == "ENCRYPTED_ENTRY"

    def test_unrelated_flag_bits_are_accepted(self):
        for flags in (0x0, 0x800, 0x8):
            data = make_raw_package([stored_entry("plain.txt", "x", flags=flags)])
            assert "plain.txt" in paths_of(inspect_package(data))


class TestCompressionMethods:
    @pytest.mark.parametrize("method", [1, 2, 3, 4, 5, 6, 9, 10, 12, 14, 18, 19, 93, 95, 98, 99])
    def test_methods_other_than_stored_and_deflate_are_rejected(self, method):
        data = make_skill_zip(files={"a.txt": "plain"}, compression=zipfile.ZIP_STORED)
        assert inspect_package(data).file_count == 2
        error = rejection(set_compression_method(data, "a.txt", method))
        assert error.reason == "UNSUPPORTED_COMPRESSION"
        assert str(method) in error.message

    def test_bzip2_archives_are_rejected(self):
        pytest.importorskip("bz2")
        data = make_skill_zip(files={"a.txt": "plain " * 100}, compression=zipfile.ZIP_BZIP2)
        assert reason_of(data) == "UNSUPPORTED_COMPRESSION"

    def test_lzma_archives_are_rejected(self):
        pytest.importorskip("lzma")
        data = make_skill_zip(files={"a.txt": "plain " * 100}, compression=zipfile.ZIP_LZMA)
        assert reason_of(data) == "UNSUPPORTED_COMPRESSION"

    def test_a_single_unsupported_entry_poisons_the_package(self):
        pytest.importorskip("bz2")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(zipfile.ZipInfo("SKILL.md"), skill_md("mixed", "1.0.0"))
            archive.writestr(zipfile.ZipInfo("ok.txt"), "fine", compress_type=zipfile.ZIP_DEFLATED)
            archive.writestr(
                zipfile.ZipInfo("bad.txt"), "bzip2 " * 50, compress_type=zipfile.ZIP_BZIP2
            )
        assert reason_of(buffer.getvalue()) == "UNSUPPORTED_COMPRESSION"

    def test_an_unsupported_directory_entry_is_rejected_too(self):
        data = make_raw_package([stored_entry("d/", method=12, external_attr=UNIX_DIRECTORY)])
        assert reason_of(data) == "UNSUPPORTED_COMPRESSION"

    def test_stored_and_deflate_are_accepted(self):
        data = build_raw_zip(
            [
                stored_entry("SKILL.md", skill_md("both", "1.0.0")),
                deflated_entry("compressed.txt", "deflated text " * 50),
            ]
        )
        parsed = inspect_package(data)
        assert manifest_of(parsed)["compressed.txt"].size == len("deflated text " * 50)


class TestDuplicates:
    def test_the_same_path_twice(self):
        data = make_zip(
            [("SKILL.md", skill_md("dup", "1.0.0")), ("a.txt", "first"), ("a.txt", "second")]
        )
        error = rejection(data)
        assert error.reason == "DUPLICATE_PATH"
        assert "a.txt" in error.message

    def test_a_duplicated_entrypoint_is_a_duplicate_not_an_ambiguity(self):
        data = make_zip(
            [("SKILL.md", skill_md("dup", "1.0.0")), ("SKILL.md", skill_md("evil", "9.9.9"))]
        )
        assert reason_of(data) == "DUPLICATE_PATH"

    def test_duplicates_inside_a_wrapper(self):
        data = make_zip(
            [("SKILL.md", skill_md("dup", "1.0.0")), ("x/y.txt", "1"), ("x/y.txt", "2")],
            wrapper="pkg",
        )
        assert reason_of(data) == "DUPLICATE_PATH"

    def test_a_file_and_a_directory_with_the_same_name(self):
        data = make_zip(
            [("SKILL.md", skill_md("dup", "1.0.0")), ("docs", "a file"), ("docs/", b"")]
        )
        assert reason_of(data) == "DUPLICATE_PATH"

    def test_a_file_used_as_a_parent_directory(self):
        data = make_zip(
            [("SKILL.md", skill_md("dup", "1.0.0")), ("docs", "a file"), ("docs/readme.md", "x")]
        )
        error = rejection(data)
        assert error.reason == "DUPLICATE_PATH"
        assert "file and directory" in error.message

    def test_a_file_used_as_a_deeper_parent_directory(self):
        data = make_zip(
            [("SKILL.md", skill_md("dup", "1.0.0")), ("a/b", "a file"), ("a/b/c/d.txt", "x")]
        )
        assert reason_of(data) == "DUPLICATE_PATH"

    def test_the_same_directory_listed_twice(self):
        data = make_zip([("SKILL.md", skill_md("dup", "1.0.0")), ("docs/", b""), ("docs/", b"")])
        assert reason_of(data) == "DUPLICATE_PATH"

    def test_paths_that_differ_only_by_case_collide(self):
        data = make_zip(
            [("SKILL.md", skill_md("dup", "1.0.0")), ("README.md", "a"), ("readme.md", "b")]
        )
        assert reason_of(data) == "DUPLICATE_PATH"

    def test_a_case_variant_of_the_entrypoint_collides(self):
        data = make_zip([("SKILL.md", skill_md("dup", "1.0.0")), ("skill.md", "shadow")])
        assert reason_of(data) == "DUPLICATE_PATH"

    def test_paths_that_differ_only_by_unicode_normalization_collide(self):
        composed = "caf" + E_ACUTE + ".txt"
        decomposed = "cafe" + COMBINING_ACUTE + ".txt"
        assert composed != decomposed
        assert unicodedata.normalize("NFC", decomposed) == composed
        data = make_zip(
            [("SKILL.md", skill_md("dup", "1.0.0")), (composed, "a"), (decomposed, "b")]
        )
        assert reason_of(data) == "DUPLICATE_PATH"

    def test_nested_paths_that_differ_only_by_case_collide(self):
        data = make_zip(
            [("SKILL.md", skill_md("dup", "1.0.0")), ("Docs/a.txt", "1"), ("docs/a.txt", "2")]
        )
        assert reason_of(data) == "DUPLICATE_PATH"

    def test_directory_entries_that_differ_only_by_case_collide(self):
        data = make_zip([("SKILL.md", skill_md("dup", "1.0.0")), ("Docs/", b""), ("docs/", b"")])
        assert reason_of(data) == "DUPLICATE_PATH"

    def test_parent_directories_that_differ_only_by_case_hold_distinct_files_safely(self):
        data = make_zip(
            [("SKILL.md", skill_md("dup", "1.0.0")), ("Docs/a.txt", "1"), ("docs/b.txt", "2")]
        )
        assert inspect_package(data).file_count == 3

    def test_similar_but_distinct_paths_are_fine(self):
        files = {
            "a.txt": "1",
            "a.txt.bak": "2",
            "a/b.txt": "3",
            "A-b.txt": "4",
            "a_b.txt": "5",
            "ab.txt": "6",
        }
        assert inspect_package(make_skill_zip(files=files)).file_count == 7

    def test_the_same_file_name_in_different_directories_is_fine(self):
        parsed = inspect_package(
            make_skill_zip(files={"a/x.txt": "1", "b/x.txt": "2", "x.txt": "3"})
        )
        assert parsed.file_count == 4

    def test_a_file_whose_name_prefixes_another_is_fine(self):
        parsed = inspect_package(
            make_skill_zip(files={"docs": "file", "docs.md": "x", "docs-extra/a.txt": "y"})
        )
        assert parsed.file_count == 4


class TestArchiveStructure:
    @pytest.mark.parametrize(
        "data",
        [
            b"",
            b"hello",
            b"PK",
            b"P",
            b"\x00" * 100,
            b"\x00" * 22,
            b"%PDF-1.7\n" + b"x" * 100,
            b"\x89PNG\r\n\x1a\n" + b"x" * 100,
            b"\x1f\x8b\x08\x00" + b"x" * 100,
            b"x" * 5000,
        ],
    )
    def test_things_that_are_not_zip_files(self, data):
        assert reason_of(data) == "NOT_A_ZIP"

    @pytest.mark.parametrize(
        "data", [b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08", b"PK\x03\x04junk"]
    )
    def test_data_that_starts_like_a_zip_but_has_no_directory_is_corrupt(self, data):
        assert reason_of(data) == "CORRUPT_ARCHIVE"

    def test_a_truncated_archive_is_corrupt(self):
        data = make_skill_zip(files={"a.txt": "x" * 200})
        assert reason_of(data[:-10]) == "CORRUPT_ARCHIVE"
        assert reason_of(data[: len(data) // 2]) == "CORRUPT_ARCHIVE"
        assert reason_of(data[:100]) == "CORRUPT_ARCHIVE"

    @pytest.mark.parametrize(
        "prefix", [b"#!/bin/sh\nexit 0\n", b"MZ\x90\x00" + bytes(60), b"\n", b"\x00"]
    )
    def test_data_in_front_of_the_first_entry_is_rejected(self, prefix):
        good = make_skill_zip(files={"a.txt": "x"})
        assert inspect_package(good).file_count == 2
        error = rejection(prefix + good)
        assert error.reason == "CORRUPT_ARCHIVE"
        assert "before" in error.message

    @pytest.mark.parametrize(
        "suffix", [b"JUNK", b"\n", b"\r\n", b"\x00", b"PK\x05\x06", bytes(100)]
    )
    def test_data_after_the_end_record_is_rejected(self, suffix):
        good = make_skill_zip(files={"a.txt": "x"})
        error = rejection(good + suffix)
        assert error.reason == "CORRUPT_ARCHIVE"

    def test_a_declared_archive_comment_is_not_trailing_data(self):
        for comment in (b"", b"x", b"made by a test", bytes(range(200))):
            data = build_raw_zip(
                [stored_entry("SKILL.md", skill_md("commented", "1.0.0"))], comment=comment
            )
            assert inspect_package(data).name == "commented"

    def test_a_comment_longer_than_declared_is_trailing_data(self):
        data = build_raw_zip(
            [stored_entry("SKILL.md", skill_md("commented", "1.0.0"))], comment=b"abc"
        )
        assert reason_of(data + b"def") == "CORRUPT_ARCHIVE"

    def test_an_archive_written_by_info_zip_is_accepted(self, tmp_path):
        zip_binary = shutil.which("zip")
        if zip_binary is None:
            pytest.skip("the zip command is not installed")
        source = tmp_path / "source" / "info-zip-skill"
        (source / "scripts").mkdir(parents=True)
        (source / "docs").mkdir()
        (source / "SKILL.md").write_text(skill_md("info-zip-skill", "1.0.0"), encoding="utf-8")
        (source / "scripts" / "run.py").write_text("print(1)\n", encoding="utf-8")
        (source / "docs" / ("caf" + E_ACUTE + ".md")).write_text("cafe\n", encoding="utf-8")
        archive = tmp_path / "out.zip"
        subprocess.run(
            [zip_binary, "-r", "-q", str(archive), "info-zip-skill"],
            cwd=tmp_path / "source",
            check=True,
        )
        parsed = inspect_package(archive.read_bytes())
        assert parsed.root_prefix == "info-zip-skill/"
        assert paths_of(parsed) == sorted(
            ["SKILL.md", "scripts/run.py", "docs/caf" + E_ACUTE + ".md"]
        )

    def test_a_gzip_stream_is_not_a_zip(self):
        import gzip

        assert reason_of(gzip.compress(make_skill_zip())) == "NOT_A_ZIP"

    def test_a_central_directory_with_a_bad_signature_is_corrupt(self):
        data = make_skill_zip(files={"a.txt": "x"})
        directory_start = data.index(b"PK\x01\x02")
        broken = data[:directory_start] + b"PK\x09\x09" + data[directory_start + 4 :]
        assert reason_of(broken) == "CORRUPT_ARCHIVE"

    def test_a_bad_local_header_signature_is_corrupt(self):
        data = make_skill_zip(files={"a.txt": "x" * 50}, compression=zipfile.ZIP_STORED)
        offset = local_header_offset(data, "a.txt")
        broken = data[:offset] + b"PK\x09\x09" + data[offset + 4 :]
        assert reason_of(broken) == "CORRUPT_ARCHIVE"

    def test_a_flipped_byte_in_stored_content_fails_the_crc_check(self):
        data = make_skill_zip(files={"a.bin": bytes(range(200))}, compression=zipfile.ZIP_STORED)
        assert inspect_package(data).file_count == 2
        error = rejection(corrupt_entry_data(data, "a.bin", index=50))
        assert error.reason == "CORRUPT_ARCHIVE"
        assert "a.bin" in error.message

    def test_a_flipped_byte_in_the_entrypoint_fails_the_crc_check(self):
        data = make_skill_zip(compression=zipfile.ZIP_STORED)
        assert reason_of(corrupt_entry_data(data, "SKILL.md", index=60)) == "CORRUPT_ARCHIVE"

    def test_a_wrong_crc_in_the_headers_is_corrupt(self):
        data = make_skill_zip(files={"a.txt": "some content"})
        assert reason_of(forge_crc(data, "a.txt", 0x12345678)) == "CORRUPT_ARCHIVE"

    def test_a_corrupt_deflate_stream_is_corrupt(self):
        content = bytes(range(256)) * 20
        data = make_skill_zip(files={"a.bin": content})
        offset = local_header_offset(data, "a.bin")
        position = offset + 30 + len(b"a.bin") + 4
        broken = bytearray(data)
        for index in range(position, position + 24):
            broken[index] ^= 0xFF
        assert reason_of(bytes(broken)) == "CORRUPT_ARCHIVE"

    def test_a_truncated_stored_entry_is_corrupt(self):
        data = make_skill_zip(files={"a.bin": bytes(range(200))}, compression=zipfile.ZIP_STORED)
        assert reason_of(forge_compressed_size(data, "a.bin", 500)) == "CORRUPT_ARCHIVE"

    def test_the_name_in_the_local_header_must_match_the_directory(self):
        data = build_raw_zip(
            [
                stored_entry("SKILL.md", skill_md("liar", "1.0.0")),
                stored_entry("harmless.txt", "payload", local_name="different.txt"),
            ]
        )
        assert reason_of(data) == "CORRUPT_ARCHIVE"

    def test_entries_sharing_their_data_are_rejected(self):
        data = make_skill_zip(
            files={"a.txt": "shared", "b.txt": "shared"}, compression=zipfile.ZIP_STORED
        )
        patched = set_local_header_offset(data, "b.txt", local_header_offset(data, "a.txt"))
        error = rejection(patched)
        assert error.reason == "CORRUPT_ARCHIVE"
        assert "shares" in error.message

    def test_entries_pointing_into_the_middle_of_other_data_are_rejected(self):
        data = make_skill_zip(
            files={"a.txt": "x" * 100, "b.txt": "y"}, compression=zipfile.ZIP_STORED
        )
        patched = set_local_header_offset(data, "b.txt", local_header_offset(data, "a.txt") + 7)
        assert reason_of(patched) == "CORRUPT_ARCHIVE"

    def test_a_directory_entry_cannot_carry_content(self):
        data = make_raw_package(
            [stored_entry("docs/", "hidden payload", external_attr=UNIX_DIRECTORY)]
        )
        error = rejection(data)
        assert error.reason == "CORRUPT_ARCHIVE"
        assert "Directory" in error.message

    def test_an_end_record_with_absurd_offsets_is_corrupt_not_a_crash(self):
        data = bytearray(make_skill_zip())
        end = data.rfind(b"PK\x05\x06")
        for position, value in ((end + 12, 0xFFFFFF00), (end + 16, 0xFFFFFF00)):
            patched = bytearray(data)
            patched[position : position + 4] = value.to_bytes(4, "little")
            assert reason_of(bytes(patched)) == "CORRUPT_ARCHIVE"

    def test_invalid_utf8_in_an_entry_name_is_corrupt(self):
        data = build_raw_zip(
            [
                stored_entry("SKILL.md", skill_md("names", "1.0.0")),
                stored_entry(b"bad-\xff-name", "x", flags=0x800),
            ]
        )
        assert reason_of(data) == "CORRUPT_ARCHIVE"

    def test_legacy_code_page_names_are_decoded(self):
        data = build_raw_zip(
            [
                stored_entry("SKILL.md", skill_md("names", "1.0.0")),
                stored_entry(b"caf\x82.txt", "x", flags=0),
            ]
        )
        assert "caf" + E_ACUTE + ".txt" in paths_of(inspect_package(data))

    def test_the_archive_is_closed_after_a_rejection(self):
        for _ in range(3):
            reason_of(make_traversal_zip("../x"))
        assert inspect_package(make_skill_zip()).name == "sample-skill"


DESCRIPTOR_FORMS = {
    "with-signature": ({"signature": True, "zip64": False}, 16),
    "without-signature": ({"signature": False, "zip64": False}, 12),
    "zip64-with-signature": ({"signature": True, "zip64": True}, 24),
    "zip64-without-signature": ({"signature": False, "zip64": True}, 20),
    "missing": (None, 0),
}


def descriptor_of(entry, form):
    options, length = DESCRIPTOR_FORMS[form]
    if options is None:
        return b""
    crc = zlib.crc32(entry.data) if entry.crc is None else entry.crc
    compressed = len(entry.data) if entry.compressed_size is None else entry.compressed_size
    uncompressed = len(entry.data) if entry.uncompressed_size is None else entry.uncompressed_size
    descriptor = data_descriptor(crc, compressed, uncompressed, **options)
    assert len(descriptor) == length
    return descriptor


def deflated_base_entries():
    return [
        layout_deflated("SKILL.md", skill_md("probe", "1.0.0")),
        layout_deflated("a.txt", "alpha" * 40),
        layout_deflated("b.txt", "bravo" * 40),
    ]


CENTRAL_ORDERS = [[2, 1, 0], [1, 2, 0], [0, 2, 1], [2, 0, 1], [1, 0, 2]]


class TestArchiveTessellation:
    @staticmethod
    def entries():
        return base_entries()

    @staticmethod
    def central_directory_names(data):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            assert archive.testzip() is None
            return archive.namelist()

    def test_the_control_archive_is_accepted(self):
        assert inspect_package(build_layout_zip(self.entries())).file_count == 3

    @pytest.mark.parametrize("streamed", [False, True])
    @pytest.mark.parametrize("position", [1, 2, 3])
    def test_a_local_header_missing_from_the_central_directory_is_rejected(
        self, position, streamed
    ):
        entries = [declare_descriptor(entry) if streamed else entry for entry in self.entries()]
        entries.insert(position, layout_stored("../../evil.txt", "owned", listed=False))
        data = build_layout_zip(entries)
        assert "../../evil.txt" not in self.central_directory_names(data)
        assert data.count(b"../../evil.txt") == 1
        error = rejection(data)
        assert error.reason == "CORRUPT_ARCHIVE"

    @pytest.mark.parametrize("position", [1, 2, 3])
    def test_a_streaming_extractor_would_have_materialized_the_hidden_entry(self, position):
        entries = self.entries()
        entries.insert(position, layout_stored("../../evil.txt", "owned", listed=False))
        data = build_layout_zip(entries)
        assert "../../evil.txt" in streaming_names(data)
        assert "../../evil.txt" not in self.central_directory_names(data)
        assert reason_of(data) == "CORRUPT_ARCHIVE"

    @pytest.mark.parametrize(
        "name", ["../../evil.txt", "__MACOSX/._ghost", ".DS_Store", "safe.txt", "docs/", "/abs"]
    )
    def test_a_hidden_local_header_is_rejected_whatever_its_name(self, name):
        entries = self.entries()
        entries.insert(2, layout_stored(name, "x", listed=False))
        assert reason_of(build_layout_zip(entries)) == "CORRUPT_ARCHIVE"

    @pytest.mark.parametrize("order", CENTRAL_ORDERS)
    def test_the_central_directory_may_list_the_entries_in_any_order(self, order):
        data = build_layout_zip(self.entries(), central_order=order)
        assert self.central_directory_names(data) == [
            ["SKILL.md", "a.txt", "b.txt"][i] for i in order
        ]
        assert inspect_package(data).file_count == 3

    @pytest.mark.parametrize("order", CENTRAL_ORDERS)
    def test_a_shuffled_central_directory_cannot_hide_unaccounted_bytes(self, order):
        entries = self.entries()
        entries[1].gap_after = b"JUNK"
        assert reason_of(build_layout_zip(entries, central_order=order)) == "CORRUPT_ARCHIVE"
        entries = self.entries()
        entries.insert(2, layout_stored("../../evil.txt", "owned", listed=False))
        data = build_layout_zip(entries, central_order=order)
        assert "../../evil.txt" not in self.central_directory_names(data)
        assert reason_of(data) == "CORRUPT_ARCHIVE"

    def test_a_hidden_local_header_in_front_of_the_first_entry_is_rejected(self):
        entries = self.entries()
        entries.insert(0, layout_stored("../../evil.txt", "owned", listed=False))
        error = rejection(build_layout_zip(entries))
        assert error.reason == "CORRUPT_ARCHIVE"
        assert "before" in error.message

    def test_a_hidden_local_header_spliced_into_a_real_archive_is_rejected(self):
        good = make_skill_zip(files={"a.txt": "alpha", "docs/b.txt": "bravo"})
        assert inspect_package(good).file_count == 3
        orphan = layout_local_record(layout_stored("../../evil.txt", "owned"))
        _, boundary = entry_extent(good, "a.txt")
        hostile = splice_bytes(good, boundary, orphan)
        assert self.central_directory_names(hostile) == self.central_directory_names(good)
        assert reason_of(hostile) == "CORRUPT_ARCHIVE"
        hostile = splice_bytes(good, central_directory_offset(good), orphan)
        assert self.central_directory_names(hostile) == self.central_directory_names(good)
        assert reason_of(hostile) == "CORRUPT_ARCHIVE"

    @pytest.mark.parametrize("index", [0, 1, 2])
    @pytest.mark.parametrize("filler", [b"\x00", b"JUNK", b"PK\x03\x04"])
    @pytest.mark.parametrize("size", [1, 7, 13, 29, 30, 64, 4096])
    def test_bytes_that_no_entry_accounts_for_are_rejected(self, index, filler, size):
        entries = self.entries()
        entries[index].gap_after = (filler * size)[:size]
        data = build_layout_zip(entries)
        assert self.central_directory_names(data) == ["SKILL.md", "a.txt", "b.txt"]
        error = rejection(data)
        assert error.reason == "CORRUPT_ARCHIVE"
        assert str(size) in error.message

    @pytest.mark.parametrize("index", [0, 1, 2])
    @pytest.mark.parametrize("surplus", [1, 10, 1000])
    def test_an_entry_that_claims_more_bytes_than_it_has_is_rejected(self, index, surplus):
        entries = self.entries()
        entries[index].compressed_size = len(entries[index].data) + surplus
        error = rejection(build_layout_zip(entries))
        assert error.reason == "CORRUPT_ARCHIVE"
        assert "overlaps" in error.message

    @pytest.mark.parametrize("index", [0, 1, 2])
    def test_an_entry_that_claims_fewer_bytes_than_it_has_leaves_an_unaccounted_tail(self, index):
        entries = self.entries()
        entries[index].compressed_size = len(entries[index].data) - 1
        assert reason_of(build_layout_zip(entries)) == "CORRUPT_ARCHIVE"

    def test_entries_overlapping_in_the_central_directory_are_rejected(self):
        data = build_layout_zip(self.entries())
        start, _ = entry_extent(data, "a.txt")
        patched = set_local_header_offset(data, "b.txt", start + 3)
        assert reason_of(patched) == "CORRUPT_ARCHIVE"

    def test_an_entry_listed_after_the_central_directory_is_rejected(self):
        data = build_layout_zip(self.entries())
        patched = set_local_header_offset(data, "b.txt", central_directory_offset(data) + 5)
        assert reason_of(patched) == "CORRUPT_ARCHIVE"

    @pytest.mark.parametrize("name", ["SKILL.md", "a.txt", "b.txt"])
    def test_a_local_header_that_lies_outside_the_archive_is_rejected(self, name):
        data = build_layout_zip(self.entries())
        for offset in (len(data), len(data) - 10, len(data) + 1000):
            assert reason_of(set_local_header_offset(data, name, offset)) == "CORRUPT_ARCHIVE"

    def test_a_header_beyond_the_end_of_the_file_is_reported_as_such(self):
        entries = self.entries()
        entries[1].compressed_size = 10_000
        data = build_layout_zip(entries)
        start, _ = entry_extent(data, "a.txt")
        beyond = start + 30 + len(b"a.txt") + 10_000
        error = rejection(set_local_header_offset(data, "b.txt", beyond))
        assert error.reason == "CORRUPT_ARCHIVE"
        assert "outside" in error.message

    @pytest.mark.parametrize("name", ["docs/", "__MACOSX/", "__MACOSX/._x", ".DS_Store", "a.txt"])
    def test_a_bad_local_signature_is_rejected_even_when_the_entry_is_never_read(self, name):
        entries = [
            *self.entries(),
            layout_directory("docs"),
            layout_directory("__MACOSX"),
            layout_stored("__MACOSX/._x", "x"),
            layout_stored(".DS_Store", "x"),
        ]
        data = build_layout_zip(entries)
        assert inspect_package(data).file_count == 3
        offset = local_header_offset(data, name)
        broken = data[:offset] + b"PK\x09\x09" + data[offset + 4 :]
        error = rejection(broken)
        assert error.reason == "CORRUPT_ARCHIVE"
        assert "signature" in error.message

    @pytest.mark.parametrize("form", list(DESCRIPTOR_FORMS))
    @pytest.mark.parametrize("index", [0, 1, 2])
    @pytest.mark.parametrize("compression", ["stored", "deflated"])
    def test_every_form_of_data_descriptor_is_accepted(self, form, index, compression):
        entries = self.entries() if compression == "stored" else deflated_base_entries()
        entries[index] = declare_descriptor(entries[index], descriptor_of(entries[index], form))
        assert inspect_package(build_layout_zip(entries)).file_count == 3

    @pytest.mark.parametrize("form", list(DESCRIPTOR_FORMS))
    @pytest.mark.parametrize("compression", ["stored", "deflated"])
    def test_every_entry_of_an_archive_may_carry_its_own_form_of_descriptor(
        self, form, compression
    ):
        entries = self.entries() if compression == "stored" else deflated_base_entries()
        entries = [declare_descriptor(entry, descriptor_of(entry, form)) for entry in entries]
        assert inspect_package(build_layout_zip(entries)).file_count == 3

    @pytest.mark.parametrize(
        "length", [1, 4, 8, 11, 13, 15, 17, 19, 21, 23, 25, 28, 29, 30, 36, 49, 64, 4096]
    )
    @pytest.mark.parametrize("index", [0, 1, 2])
    def test_a_gap_that_is_not_a_data_descriptor_is_rejected_even_when_one_is_declared(
        self, length, index
    ):
        entries = self.entries()
        entries[index] = declare_descriptor(entries[index], bytes(length))
        error = rejection(build_layout_zip(entries))
        assert error.reason == "CORRUPT_ARCHIVE"
        assert str(length) in error.message

    @pytest.mark.parametrize("length", [12, 16, 20, 24])
    @pytest.mark.parametrize("index", [0, 1, 2])
    def test_a_descriptor_after_an_entry_that_does_not_declare_one_is_rejected(self, length, index):
        entries = self.entries()
        entries[index].descriptor = bytes(length)
        assert reason_of(build_layout_zip(entries)) == "CORRUPT_ARCHIVE"

    def test_a_whole_local_header_cannot_hide_where_a_descriptor_belongs(self):
        hidden = layout_local_record(layout_stored("../../evil.txt", "owned"))
        assert len(hidden) > 24
        entries = self.entries()
        entries[1] = declare_descriptor(entries[1], hidden)
        data = build_layout_zip(entries)
        assert "../../evil.txt" not in self.central_directory_names(data)
        assert reason_of(data) == "CORRUPT_ARCHIVE"

    def test_a_declared_descriptor_does_not_excuse_a_hidden_header_after_it(self):
        entries = self.entries()
        valid = data_descriptor(zlib.crc32(b"alpha"), 5, 5)
        entries[1] = declare_descriptor(entries[1], valid)
        entries[1].gap_after = layout_local_record(layout_stored("../../evil.txt", "owned"))
        data = build_layout_zip(entries)
        assert "../../evil.txt" not in self.central_directory_names(data)
        assert reason_of(data) == "CORRUPT_ARCHIVE"


SIGNED_FORMS = ["with-signature", "zip64-with-signature"]
UNSIGNED_FORMS = ["without-signature", "zip64-without-signature"]
PRESENT_FORMS = [form for form in DESCRIPTOR_FORMS if form != "missing"]
DESCRIPTOR_SIGNATURE = b"PK\x07\x08"


def forged_descriptor(entry, form, *, crc=None, compressed=None, uncompressed=None, head=None):
    options, _ = DESCRIPTOR_FORMS[form]
    real_crc = zlib.crc32(entry.data) if entry.crc is None else entry.crc
    real_compressed = len(entry.data) if entry.compressed_size is None else entry.compressed_size
    real_uncompressed = (
        len(entry.data) if entry.uncompressed_size is None else entry.uncompressed_size
    )
    descriptor = data_descriptor(
        real_crc if crc is None else crc,
        real_compressed if compressed is None else compressed,
        real_uncompressed if uncompressed is None else uncompressed,
        **options,
    )
    if head is not None:
        descriptor = head + descriptor[len(head) :]
    return descriptor


def descriptor_spans(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = sorted(archive.infolist(), key=lambda info: info.header_offset)
        boundaries = [info.header_offset for info in infos[1:]] + [archive.start_dir]
        spans = []
        for info, boundary in zip(infos, boundaries, strict=True):
            if not info.flag_bits & DESCRIPTOR_FLAG:
                continue
            name_length, extra_length = struct.unpack_from("<HH", data, info.header_offset + 26)
            start = info.header_offset + 30 + name_length + extra_length + info.compress_size
            spans.append((info.orig_filename, start, boundary - start))
        return spans


def flipped(data, position, mask=0xFF):
    patched = bytearray(data)
    patched[position] ^= mask
    return bytes(patched)


class TestDataDescriptorContent:
    CONTROL_FORMS = PRESENT_FORMS

    @staticmethod
    def package(entries):
        return build_layout_zip(entries)

    def declared(self, form, index, compression="stored", **forgery):
        entries = base_entries() if compression == "stored" else deflated_base_entries()
        descriptor = forged_descriptor(entries[index], form, **forgery)
        entries[index] = declare_descriptor(entries[index], descriptor)
        return self.package(entries)

    @pytest.mark.parametrize("compression", ["stored", "deflated"])
    @pytest.mark.parametrize("index", [0, 1, 2])
    @pytest.mark.parametrize("form", PRESENT_FORMS)
    def test_the_control_descriptors_are_accepted(self, form, index, compression):
        data = self.declared(form, index, compression)
        assert inspect_package(data).file_count == 3

    @pytest.mark.parametrize("compression", ["stored", "deflated"])
    @pytest.mark.parametrize("index", [0, 1, 2])
    @pytest.mark.parametrize("form", PRESENT_FORMS)
    @pytest.mark.parametrize(
        "forgery",
        [
            {"crc": 0},
            {"crc": 0xFFFFFFFF},
            {"crc": 1},
            {"compressed": 0},
            {"compressed": 1},
            {"compressed": 0xFFFFFFFF},
            {"uncompressed": 0},
            {"uncompressed": 1},
            {"uncompressed": 0xFFFFFFFF},
        ],
        ids=lambda forgery: "-".join(f"{key}={value:#x}" for key, value in forgery.items()),
    )
    def test_a_descriptor_that_disagrees_with_the_central_directory_is_rejected(
        self, form, index, compression, forgery
    ):
        real = base_entries()[index] if compression == "stored" else deflated_base_entries()[index]
        real_values = {
            "crc": zlib.crc32(real.data) if real.crc is None else real.crc,
            "compressed": len(real.data) if real.compressed_size is None else real.compressed_size,
            "uncompressed": len(real.data)
            if real.uncompressed_size is None
            else real.uncompressed_size,
        }
        (key, value), *_ = forgery.items()
        if real_values[key] == value:
            pytest.skip("the forged value equals the real one for this entry")
        error = rejection(self.declared(form, index, compression, **forgery))
        assert error.reason == "CORRUPT_ARCHIVE"
        assert "descriptor" in error.message
        assert "central directory" in error.message

    @pytest.mark.parametrize("index", [0, 1, 2])
    @pytest.mark.parametrize("form", PRESENT_FORMS)
    def test_the_sizes_must_come_in_the_declared_order(self, form, index):
        entries = deflated_base_entries()
        entry = entries[index]
        assert len(entry.data) != entry.uncompressed_size
        descriptor = forged_descriptor(
            entry, form, compressed=entry.uncompressed_size, uncompressed=len(entry.data)
        )
        entries[index] = declare_descriptor(entry, descriptor)
        assert reason_of(self.package(entries)) == "CORRUPT_ARCHIVE"

    @pytest.mark.parametrize("index", [0, 1, 2])
    @pytest.mark.parametrize("form", PRESENT_FORMS)
    def test_a_descriptor_that_belongs_to_another_entry_is_rejected(self, form, index):
        entries = base_entries()
        other = entries[(index + 1) % 3]
        entries[index] = declare_descriptor(entries[index], descriptor_of(other, form))
        error = rejection(self.package(entries))
        assert error.reason == "CORRUPT_ARCHIVE"
        assert "descriptor" in error.message

    @pytest.mark.parametrize("index", [0, 1, 2])
    @pytest.mark.parametrize("form", SIGNED_FORMS)
    @pytest.mark.parametrize(
        "head",
        [b"PK\x07\x09", b"PK\x03\x04", b"\x00\x00\x00\x00", b"pk\x07\x08", b"PK\x08\x07"],
    )
    def test_a_descriptor_with_a_wrong_signature_is_rejected(self, form, index, head):
        error = rejection(self.declared(form, index, head=head))
        assert error.reason == "CORRUPT_ARCHIVE"
        assert "signature" in error.message

    @pytest.mark.parametrize("index", [0, 1, 2])
    @pytest.mark.parametrize("length", [12, 16, 20, 24])
    @pytest.mark.parametrize("filler", [b"\x00", b"\xff", b"PK\x03\x04", b"PK\x07\x08"])
    def test_padding_of_any_descriptor_length_is_rejected(self, length, index, filler):
        entries = base_entries()
        padding = (filler * length)[:length]
        entries[index] = declare_descriptor(entries[index], padding)
        error = rejection(self.package(entries))
        assert error.reason == "CORRUPT_ARCHIVE"

    @pytest.mark.parametrize("index", [0, 1, 2])
    @pytest.mark.parametrize("length", [12, 16, 20, 24])
    def test_seeded_random_padding_of_any_descriptor_length_is_rejected(self, length, index):
        generator = random.Random(1000 * length + index)
        for _ in range(25):
            entries = base_entries()
            padding = generator.randbytes(length)
            entries[index] = declare_descriptor(entries[index], padding)
            assert reason_of(self.package(entries)) == "CORRUPT_ARCHIVE"

    @pytest.mark.parametrize("index", [0, 1, 2])
    def test_a_partial_local_header_cannot_hide_in_the_descriptor_slot(self, index):
        hidden = layout_local_record(layout_stored("../../evil.txt", "owned"))[:24]
        assert hidden.startswith(b"PK\x03\x04")
        entries = base_entries()
        entries[index] = declare_descriptor(entries[index], hidden)
        assert reason_of(self.package(entries)) == "CORRUPT_ARCHIVE"

    @pytest.mark.parametrize("index", [0, 1, 2])
    @pytest.mark.parametrize("high", [1, 0x100, 0xFFFFFFFF])
    def test_a_wide_descriptor_whose_high_halves_are_not_zero_is_rejected(self, index, high):
        entries = base_entries()
        raw = entries[index].data
        descriptor = struct.pack(
            "<IIQQ",
            0x08074B50,
            zlib.crc32(raw),
            (high << 32) | len(raw),
            len(raw),
        )
        entries[index] = declare_descriptor(entries[index], descriptor)
        assert reason_of(self.package(entries)) == "CORRUPT_ARCHIVE"

    @pytest.mark.parametrize("index", [0, 1, 2])
    def test_a_narrow_descriptor_padded_to_the_wide_length_is_rejected(self, index):
        entries = base_entries()
        entry = entries[index]
        narrow = data_descriptor(zlib.crc32(entry.data), len(entry.data), len(entry.data))
        entries[index] = declare_descriptor(entry, narrow + bytes(8))
        assert reason_of(self.package(entries)) == "CORRUPT_ARCHIVE"

    @pytest.mark.parametrize("index", [0, 1, 2])
    def test_a_bit_three_entry_without_any_descriptor_bytes_hides_nothing_and_is_accepted(
        self, index
    ):
        entries = base_entries()
        entries[index] = declare_descriptor(entries[index], b"")
        assert inspect_package(self.package(entries)).file_count == 3

    def test_the_signature_is_optional_but_not_negotiable_in_the_signed_lengths(self):
        entries = base_entries()
        entry = entries[1]
        unsigned = data_descriptor(zlib.crc32(entry.data), 5, 5, signature=False)
        signed = data_descriptor(zlib.crc32(entry.data), 5, 5)
        entries[1] = declare_descriptor(entry, unsigned)
        assert inspect_package(self.package(entries)).file_count == 3
        entries = base_entries()
        entries[1] = declare_descriptor(entries[1], signed)
        assert inspect_package(self.package(entries)).file_count == 3
        entries = base_entries()
        shifted = unsigned + b"\x00" * 4
        entries[1] = declare_descriptor(entries[1], shifted)
        assert reason_of(self.package(entries)) == "CORRUPT_ARCHIVE"

    @pytest.mark.parametrize("name", [".DS_Store", "__MACOSX/._a", "docs/"])
    @pytest.mark.parametrize("form", PRESENT_FORMS)
    def test_ignored_entries_and_directories_get_the_same_scrutiny(self, name, form):
        carrier = layout_stored(name, b"" if name.endswith("/") else b"junk")
        descriptor = forged_descriptor(carrier, form)
        control = [*base_entries(), declare_descriptor(carrier, descriptor)]
        assert inspect_package(self.package(control)).file_count == 3
        hostile_carrier = layout_stored(name, b"" if name.endswith("/") else b"junk")
        forged = forged_descriptor(hostile_carrier, form, crc=0x12345678)
        hostile = [*base_entries(), declare_descriptor(hostile_carrier, forged)]
        assert reason_of(self.package(hostile)) == "CORRUPT_ARCHIVE"


STREAMED_BUILDERS = [
    "python-stream-stored",
    "python-stream-deflate",
    "python-stream-directories",
    "python-stream-junk",
    "python-stream-zip64",
    "python-stream-zip64-stored",
    "macos-archive-utility",
    "descriptor-with-signature",
    "descriptor-without-signature",
    "descriptor-zip64",
    "descriptor-zip64-without-signature",
    "descriptor-deflated-with-comment",
]


class TestEveryByteOfEveryDescriptorMatters:
    @pytest.mark.parametrize("label", STREAMED_BUILDERS)
    def test_the_fixture_really_has_descriptors_and_is_accepted_untouched(self, label):
        data = ARCHIVE_BUILDERS[label][0]()
        spans = descriptor_spans(data)
        assert spans
        assert all(length in (12, 16, 20, 24) for _, _, length in spans)
        assert inspect_package(data).name == "legit-package"

    @pytest.mark.parametrize("label", STREAMED_BUILDERS)
    def test_changing_any_single_byte_of_any_descriptor_is_rejected(self, label):
        data = ARCHIVE_BUILDERS[label][0]()
        checked = 0
        for name, start, length in descriptor_spans(data):
            for offset in range(length):
                hostile = flipped(data, start + offset)
                error = rejection(hostile)
                assert error.reason == "CORRUPT_ARCHIVE", (name, offset)
                checked += 1
        assert checked >= 12 * len(LEGIT_FILES) // 2

    @pytest.mark.parametrize("label", STREAMED_BUILDERS)
    def test_changing_any_single_bit_of_the_checksum_or_sizes_is_rejected(self, label):
        data = ARCHIVE_BUILDERS[label][0]()
        for name, start, length in descriptor_spans(data):
            body = start + (4 if length in (16, 24) else 0)
            for offset in range(length - (4 if length in (16, 24) else 0)):
                for bit in range(8):
                    hostile = flipped(data, body + offset, 1 << bit)
                    assert reason_of(hostile) == "CORRUPT_ARCHIVE", (name, offset, bit)

    @pytest.mark.parametrize("gap", [12, 16, 20, 24])
    @pytest.mark.parametrize("label", STREAMED_BUILDERS)
    def test_a_descriptor_slot_beyond_the_end_of_the_file_is_corrupt_not_a_crash(self, label, gap):
        data = ARCHIVE_BUILDERS[label][0]()
        far = len(data) + 4096
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            ordered = sorted(archive.infolist(), key=lambda info: info.header_offset)
        flagged = [
            (info, following)
            for info, following in itertools.pairwise(ordered)
            if info.flag_bits & DESCRIPTOR_FLAG
        ]
        assert flagged
        for info, following in flagged:
            name_length, extra_length = struct.unpack_from("<HH", data, info.header_offset + 26)
            data_start = info.header_offset + 30 + name_length + extra_length
            hostile = set_local_header_offset(data, following.orig_filename, far)
            hostile = set_central_fields(
                hostile, info.orig_filename, compressed_size=far - gap - data_start
            )
            with pytest.raises(InvalidPackageError) as caught:
                inspect_package(hostile)
            assert caught.value.reason == "CORRUPT_ARCHIVE"


class TestDescriptorsFromRealWriters:
    def test_python_streaming_writers_produce_what_the_inspector_checks(self):
        for label in ("python-stream-stored", "python-stream-deflate", "python-stream-zip64"):
            data = ARCHIVE_BUILDERS[label][0]()
            lengths = {length for _, _, length in descriptor_spans(data)}
            assert lengths == ({24} if label.endswith("zip64") else {16})
            assert inspect_package(data).name == "legit-package"

    @pytest.mark.parametrize(
        "options",
        [["-fd"], ["-fd", "-0"], ["-fd", "-9"], ["-fd", "-X"], ["-fd", "-D"]],
        ids=["forced", "forced-stored", "forced-best", "forced-no-attributes", "forced-no-dirs"],
    )
    @pytest.mark.parametrize("layout", ["wrapped", "flat"])
    def test_info_zip_with_forced_descriptors_is_accepted(self, tmp_path, options, layout):
        source = tmp_path / LEGIT_WRAPPER
        write_tree(source, LEGIT_FILES)
        archive = tmp_path / "out.zip"
        target = LEGIT_WRAPPER if layout == "wrapped" else "."
        cwd = tmp_path if layout == "wrapped" else source
        info_zip("-r", *options, "-q", str(archive), target, cwd=cwd)
        data = archive.read_bytes()
        spans = descriptor_spans(data)
        assert len(spans) >= len(LEGIT_FILES)
        assert {length for _, _, length in spans} == {16}
        parsed = inspect_package(data)
        assert parsed.root_prefix == (LEGIT_WRAPPER + "/" if layout == "wrapped" else "")
        assert paths_of(parsed) == sorted(LEGIT_FILES)
        for entry in parsed.manifest:
            assert read_package_file(data, parsed.root_prefix, entry.path) == legit_bytes(
                LEGIT_FILES[entry.path]
            )

    @pytest.mark.parametrize(
        "options",
        [[], ["-0"], ["-9"], ["-X"], ["-D"], ["-fz-"]],
        ids=[
            "pipe",
            "pipe-stored",
            "pipe-best",
            "pipe-no-attributes",
            "pipe-no-dirs",
            "pipe-no-zip64",
        ],
    )
    def test_info_zip_in_pipe_mode_is_accepted_and_its_descriptors_are_checked(
        self, tmp_path, options
    ):
        write_tree(tmp_path / LEGIT_WRAPPER, LEGIT_FILES)
        data = info_zip("-r", *options, "-q", "-", LEGIT_WRAPPER, cwd=tmp_path)
        spans = descriptor_spans(data)
        assert len(spans) >= len(LEGIT_FILES)
        assert {length for _, _, length in spans} == {16}
        assert inspect_package(data).root_prefix == LEGIT_WRAPPER + "/"
        hostile = flipped(data, spans[0][1] + 5)
        assert reason_of(hostile) == "CORRUPT_ARCHIVE"

    @pytest.mark.parametrize(
        ("options", "length"), [([], 24), (["-fz-"], 16)], ids=["zip64-by-default", "no-zip64"]
    )
    def test_info_zip_streaming_its_standard_input_writes_descriptors_that_check_out(
        self, tmp_path, options, length
    ):
        data = info_zip("-q", *options, "-", "-", cwd=tmp_path, stdin=b"streamed line\n" * 100)
        spans = descriptor_spans(data)
        assert [(name, size) for name, _, size in spans] == [("-", length)]
        archive = inspector._open_archive(data, 1 << 20)
        try:
            inspector._check_central_directory(data, archive)
            headers = inspector._check_tessellation(data, archive)
        finally:
            archive.close()
        assert len(headers) == 1
        tampered = flipped(data, spans[0][1] + length - 1)
        archive = inspector._open_archive(tampered, 1 << 20)
        try:
            with pytest.raises(InvalidPackageError) as caught:
                inspector._check_tessellation(tampered, archive)
        finally:
            archive.close()
        assert caught.value.reason == "CORRUPT_ARCHIVE"


DISAGREEMENTS = {
    "method": ({"local_method": zipfile.ZIP_DEFLATED}, "compression method"),
    "crc": ({"local_crc": 0x12345678}, "sizes or the checksum"),
    "compressed-size": ({"local_compressed_size": 4}, "sizes or the checksum"),
    "uncompressed-size": ({"local_uncompressed_size": 4}, "sizes or the checksum"),
    "descriptor-flag-only-locally": ({"local_flags": DESCRIPTOR_FLAG}, "flags"),
    "descriptor-flag-only-centrally": ({"flags": DESCRIPTOR_FLAG, "local_flags": 0}, "flags"),
    "encryption-flag-only-locally": ({"local_flags": 0x1}, "flags"),
    "strong-encryption-flag-only-locally": ({"local_flags": 0x40}, "flags"),
}


class TestLocalHeadersAgreeWithTheCentralDirectory:
    @pytest.mark.parametrize("kind", list(DISAGREEMENTS))
    @pytest.mark.parametrize("name", ["a.txt", "__MACOSX/._a.txt", ".DS_Store"])
    def test_a_local_header_that_contradicts_the_central_directory_is_rejected(self, kind, name):
        others = [base_entries()[0], base_entries()[2]]
        control = [*others, layout_stored(name, "alpha")]
        assert inspect_package(build_layout_zip(control)).name == "probe"
        overrides, fragment = DISAGREEMENTS[kind]
        hostile = [*others, layout_stored(name, "alpha", **overrides)]
        error = rejection(build_layout_zip(hostile))
        assert error.reason == "CORRUPT_ARCHIVE"
        assert "local header" in error.message
        assert fragment in error.message

    def test_a_shorter_local_size_cannot_hide_an_entry_inside_the_declared_extent(self):
        hidden = layout_local_record(layout_stored("../../evil.txt", "owned"))
        carrier = layout_stored(
            "a.txt",
            b"alpha" + hidden,
            local_compressed_size=5,
            local_uncompressed_size=5,
            local_crc=zlib.crc32(b"alpha"),
        )
        data = build_layout_zip([base_entries()[0], carrier])
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            assert archive.namelist() == ["SKILL.md", "a.txt"]
            assert archive.testzip() is None
        assert streaming_names(data) == ["SKILL.md", "a.txt", "../../evil.txt"]
        error = rejection(data)
        assert error.reason == "CORRUPT_ARCHIVE"
        assert "local header" in error.message

    @pytest.mark.parametrize("field", ["name_length", "extra_length"])
    @pytest.mark.parametrize("value", [1, 7, 255, 65535])
    @pytest.mark.parametrize("name", ["SKILL.md", "b.txt"])
    def test_a_local_header_lying_about_its_lengths_is_rejected(self, field, value, name):
        data = build_layout_zip(base_entries())
        assert inspect_package(data).file_count == 3
        error = rejection(set_local_fields(data, name, **{field: value}))
        assert error.reason == "CORRUPT_ARCHIVE"

    def test_lengths_that_trade_name_bytes_for_extra_bytes_still_name_a_different_entry(self):
        data = build_layout_zip(base_entries())
        patched = set_local_fields(data, "a.txt", name_length=4, extra_length=1)
        error = rejection(patched)
        assert error.reason == "CORRUPT_ARCHIVE"
        assert "different entry" in error.message

    def test_a_longer_local_size_cannot_swallow_the_next_entry(self):
        entries = base_entries()
        entries[1] = layout_stored("a.txt", "alpha", local_compressed_size=5 + 40)
        assert reason_of(build_layout_zip(entries)) == "CORRUPT_ARCHIVE"

    @pytest.mark.parametrize(
        "name", ["c.txt", "docs/", "__MACOSX/._c", "__MACOSX/", ".DS_Store", "Thumbs.db/"]
    )
    @pytest.mark.parametrize("variant", ["longer", "extended", "same-length", "shorter"])
    def test_a_local_name_that_differs_from_the_central_name_is_rejected(self, name, variant):
        suffix = "/" if name.endswith("/") else ""
        stem = name.rstrip("/")

        def build(local_name):
            if suffix:
                entry = layout_directory(name, local_name=local_name)
            else:
                entry = layout_stored(name, "x", local_name=local_name)
            return build_layout_zip([*base_entries(), entry])

        assert inspect_package(build(None)).name == "probe"
        local = {
            "longer": "../../evil.sh" + suffix,
            "extended": name + ("" if suffix else "/") + "../../evil.sh",
            "same-length": "z" * len(stem) + suffix,
            "shorter": stem[:-1] + suffix,
        }[variant]
        assert (variant == "extended") == local.startswith(name)
        error = rejection(build(local))
        assert error.reason == "CORRUPT_ARCHIVE"
        if variant in ("same-length", "extended"):
            assert "different entry" in error.message

    def test_a_traversal_in_the_local_name_of_an_ignored_entry_is_never_materialized(self):
        entry = layout_stored("__MACOSX/._x", "payload", local_name="../../evil.sh")
        data = build_layout_zip([*base_entries(), entry])
        assert "../../evil.sh" in streaming_names(data)
        assert "../../evil.sh" not in " ".join(zipfile.ZipFile(io.BytesIO(data)).namelist())
        assert reason_of(data) == "CORRUPT_ARCHIVE"

    def test_a_legacy_code_page_name_matches_its_local_copy(self):
        entry = LayoutEntry(b"caf\x82.txt", b"x", flags=0)
        parsed = inspect_package(build_layout_zip([*base_entries(), entry]))
        assert "caf" + E_ACUTE + ".txt" in paths_of(parsed)

    def test_the_utf8_flag_is_free_to_differ_when_the_name_bytes_are_identical(self):
        entry = layout_stored("plain.txt", "x", local_flags=0x800)
        assert "plain.txt" in paths_of(inspect_package(build_layout_zip([*base_entries(), entry])))


class TestLocalZip64Sizes:
    @staticmethod
    def zip64_entry(file_size=5, compress_size=5, *, extra=None, **overrides):
        fields = {
            "local_compressed_size": ZIP64_MARKER,
            "local_uncompressed_size": ZIP64_MARKER,
            "local_extra": (zip64_extra(file_size, compress_size) if extra is None else extra),
        }
        fields.update(overrides)
        return layout_stored("a.txt", "alpha", **fields)

    def package(self, entry):
        return build_layout_zip([base_entries()[0], entry])

    def test_sentinels_resolved_through_the_zip64_field_are_accepted(self):
        parsed = inspect_package(self.package(self.zip64_entry()))
        assert paths_of(parsed) == ["SKILL.md", "a.txt"]

    def test_the_zip64_sizes_are_read_in_their_documented_order(self):
        content = "alpha " * 50
        packed = len(layout_deflated("a.txt", content).data)
        assert packed != len(content)

        def deflated(file_size, compress_size):
            return layout_deflated(
                "a.txt",
                content,
                local_compressed_size=ZIP64_MARKER,
                local_uncompressed_size=ZIP64_MARKER,
                local_extra=zip64_extra(file_size, compress_size),
            )

        assert inspect_package(self.package(deflated(len(content), packed))).file_count == 2
        assert reason_of(self.package(deflated(packed, len(content)))) == "CORRUPT_ARCHIVE"

    def test_the_zip64_field_may_follow_other_records(self):
        other = struct.pack("<HH", 0x9999, 6) + bytes(6)
        entry = self.zip64_entry(extra=other + zip64_extra(5, 5) + other)
        assert inspect_package(self.package(entry)).file_count == 2

    @pytest.mark.parametrize(
        "extra",
        [
            zip64_extra(5, 6),
            zip64_extra(6, 5),
            zip64_extra(0, 0),
            b"",
            struct.pack("<HHQ", 1, 8, 5),
            struct.pack("<HH", 0x9999, 4) + bytes(4),
            b"\x01\x00\x10",
            struct.pack("<HH", 1, 100) + bytes(16),
            struct.pack("<HH", 1, 100) + struct.pack("<QQ", 5, 5),
            struct.pack("<HH", 0x9999, 2000) + zip64_extra(5, 5),
        ],
        ids=[
            "wrong-compressed",
            "wrong-uncompressed",
            "zeros",
            "missing",
            "too-short",
            "unrelated-record",
            "truncated-header",
            "record-overruns",
            "record-overruns-with-valid-sizes",
            "record-hides-the-field",
        ],
    )
    def test_sentinels_that_do_not_resolve_to_the_central_sizes_are_rejected(self, extra):
        error = rejection(self.package(self.zip64_entry(extra=extra)))
        assert error.reason == "CORRUPT_ARCHIVE"
        assert "disagrees" in error.message

    def test_a_zip64_field_too_short_to_hold_both_sizes_does_not_borrow_the_entry_data(self):
        content = struct.pack("<Q", 8)
        short = layout_stored(
            "a.txt",
            content,
            local_compressed_size=ZIP64_MARKER,
            local_uncompressed_size=ZIP64_MARKER,
            local_extra=struct.pack("<HHQ", 1, 8, 8),
        )
        error = rejection(self.package(short))
        assert error.reason == "CORRUPT_ARCHIVE"
        whole = layout_stored(
            "a.txt",
            content,
            local_compressed_size=ZIP64_MARKER,
            local_uncompressed_size=ZIP64_MARKER,
            local_extra=zip64_extra(8, 8),
        )
        assert inspect_package(self.package(whole)).file_count == 2

    @pytest.mark.parametrize("which", ["compressed", "uncompressed"])
    def test_a_single_sentinel_is_rejected(self, which):
        sizes = {"local_compressed_size": 5, "local_uncompressed_size": 5}
        sizes[f"local_{which}_size"] = ZIP64_MARKER
        entry = layout_stored("a.txt", "alpha", local_extra=zip64_extra(5, 5), **sizes)
        assert reason_of(self.package(entry)) == "CORRUPT_ARCHIVE"

    def test_the_search_for_the_zip64_field_is_bounded(self):
        filler = struct.pack("<HH", 0x9999, 0)
        within = self.zip64_entry(extra=filler * 63 + zip64_extra(5, 5))
        assert inspect_package(self.package(within)).file_count == 2
        beyond = self.zip64_entry(extra=filler * 64 + zip64_extra(5, 5))
        assert reason_of(self.package(beyond)) == "CORRUPT_ARCHIVE"

    def test_a_long_chain_of_empty_records_is_rejected_quickly(self):
        endless = struct.pack("<HH", 0x9999, 0) * 16_000
        entry = self.zip64_entry(extra=endless + zip64_extra(5, 5))
        started = time.process_time()
        assert reason_of(self.package(entry)) == "CORRUPT_ARCHIVE"
        assert time.process_time() - started < 1

    def test_sentinels_on_an_entry_with_a_data_descriptor_are_not_interpreted(self):
        entry = layout_stored(
            "a.txt",
            "alpha",
            streamed=True,
            local_compressed_size=ZIP64_MARKER,
            local_uncompressed_size=ZIP64_MARKER,
            local_extra=zip64_extra(0, 0),
        )
        assert inspect_package(self.package(entry)).file_count == 2


class TestCentralDirectoryPlacement:
    @staticmethod
    def relocated(data, delta):
        end = data.rfind(b"PK\x05\x06")
        offset = struct.unpack_from("<I", data, end + 16)[0]
        patched = bytearray(data)
        struct.pack_into("<I", patched, end + 16, offset + delta)
        for central, _, local in central_records(data):
            struct.pack_into("<I", patched, central + 42, local + delta)
        return bytes(patched)

    @pytest.mark.parametrize("delta", [1, 5, 100, 4096])
    def test_the_declared_central_directory_offset_must_be_the_real_one(self, delta):
        good = build_layout_zip(base_entries())
        assert inspect_package(good).file_count == 3
        relocated = self.relocated(good, delta)
        with zipfile.ZipFile(io.BytesIO(relocated)) as archive:
            assert archive.namelist() == ["SKILL.md", "a.txt", "b.txt"]
            assert archive.testzip() is None
        error = rejection(relocated)
        assert error.reason == "CORRUPT_ARCHIVE"
        assert "end record" in error.message

    def test_zip64_archives_keep_the_declared_offset_in_their_own_record(self):
        data = make_zip64_zip(LEGIT_FILES)
        assert inspect_package(data).name == "legit-package"
        data = make_stream_zip(LEGIT_FILES, force_zip64=True)
        assert inspect_package(data).name == "legit-package"

    def test_junk_in_front_of_an_archive_without_entries_is_rejected(self):
        assert reason_of(make_zip({})) == "MISSING_ENTRYPOINT"
        assert reason_of(b"JUNK" + make_zip({})) == "CORRUPT_ARCHIVE"


class TestCentralDirectoryWalk:
    @staticmethod
    def archive():
        return build_raw_zip(
            [stored_entry("SKILL.md", skill_md("cd-walk", "1.0.0")), stored_entry("a", b"x")]
        )

    @staticmethod
    def listed_by_zipfile(data):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                return archive.namelist()
        except zipfile.BadZipFile:
            return None

    @classmethod
    def stub(cls):
        class Opened:
            start_dir = end_fields(cls.archive())["offset"]

            @staticmethod
            def infolist():
                return [None, None]

        return Opened()

    def test_the_control_archive_is_accepted(self):
        data = self.archive()
        fields = end_fields(data)
        assert paths_of(inspect_package(data)) == ["SKILL.md", "a"]
        assert (fields["disk"], fields["directory_disk"]) == (0, 0)
        assert (fields["entries_here"], fields["entries"]) == (2, 2)

    @pytest.mark.parametrize("field", ["name", "extra", "comment"])
    def test_a_last_record_that_overflows_into_the_end_record_is_rejected(self, field):
        data = overflow_central_directory(self.archive(), field=field)
        assert self.listed_by_zipfile(data) == ["SKILL.md", "a"]
        error = rejection(data)
        assert error.reason == "CORRUPT_ARCHIVE"
        assert "do not end where the end record begins" in error.message

    def test_the_overflow_hides_a_traversal_from_zipfile_but_not_from_a_length_honouring_reader(
        self,
    ):
        data = overflow_central_directory(self.archive(), tail=b"/../../e")
        assert self.listed_by_zipfile(data) == ["SKILL.md", "a"]
        names = length_honouring_names(data, 2)
        assert names[0] == b"SKILL.md"
        assert names[1].endswith(b"/../../e")
        assert b".." in names[1].split(b"/")
        assert reason_of(data) == "CORRUPT_ARCHIVE"

    @pytest.mark.parametrize("junk", [b"\x00", b"JUNK", bytes(45), bytes(64)])
    def test_bytes_after_the_last_record_are_rejected(self, junk):
        data = self.archive()
        end = end_record_offset(data)
        padded = data[:end] + junk + data[end:]
        padded = set_end_fields(padded, size=end_fields(data)["size"] + len(junk))
        assert reason_of(padded) == "CORRUPT_ARCHIVE"

    @pytest.mark.parametrize(
        "changes",
        [
            {"entries": 3},
            {"entries": 1},
            {"entries": 0},
            {"entries_here": 3},
            {"entries_here": 1},
            {"entries_here": 0},
            {"entries_here": 0xFFFF, "entries": 0xFFFF},
            {"entries_here": 1, "entries": 3},
            {"disk": 1},
            {"directory_disk": 1},
            {"disk": 1, "directory_disk": 1},
            {"disk": 0xFFFF},
        ],
        ids=lambda changes: ",".join(f"{key}={value}" for key, value in changes.items()),
    )
    def test_the_end_record_must_agree_with_the_records_of_the_directory(self, changes):
        data = set_end_fields(self.archive(), **changes)
        assert self.listed_by_zipfile(data) == ["SKILL.md", "a"]
        error = rejection(data)
        assert error.reason == "CORRUPT_ARCHIVE"
        assert "end record" in error.message

    @pytest.mark.parametrize(
        "markers",
        [
            (),
            ("offset",),
            ("size", "offset"),
            ("disk", "directory_disk", "entries_here", "entries", "size", "offset"),
        ],
    )
    def test_a_zip64_end_record_with_consistent_values_is_accepted(self, markers):
        data = with_zip64_end(self.archive(), markers=markers)
        assert has_zip64_end(data)
        assert paths_of(inspect_package(data)) == ["SKILL.md", "a"]

    @pytest.mark.parametrize(
        "options",
        [
            {"narrow": {"entries": 5}},
            {"narrow": {"size": 7}},
            {"narrow": {"disk": 1}},
            {"wide": {"entries": 5, "entries_here": 5}, "markers": ("entries", "entries_here")},
            {"wide": {"entries": 1}, "markers": ("entries",)},
            {"wide": {"disk": 1}, "markers": ("disk",)},
            {"wide": {"directory_disk": 1}, "markers": ("directory_disk",)},
            {"wide": {"size": 5}, "markers": ("size",)},
            {"wide": {"offset": 5}, "markers": ("offset",)},
            {"record": False},
            {"locator_offset": 3},
            {"locator_disk": 1},
            {"disks": 2},
            {"record_signature": b"PK\x06\x09"},
            {"record_size": 45},
            {"record_size": 0},
            {"record_size": 0xFFFFFFFFFFFFFFFF},
        ],
        ids=lambda options: ",".join(options),
    )
    def test_a_zip64_end_record_that_disagrees_with_the_archive_is_rejected(self, options):
        data = with_zip64_end(self.archive(), **options)
        assert reason_of(data) == "CORRUPT_ARCHIVE"

    @pytest.mark.parametrize(
        "options",
        [
            {"narrow": {"entries": 5}},
            {"narrow": {"size": 7}},
            {"narrow": {"disk": 1}},
            {"wide": {"entries": 5, "entries_here": 5}, "markers": ("entries", "entries_here")},
            {"wide": {"disk": 1}, "markers": ("disk",)},
            {"wide": {"directory_disk": 1}, "markers": ("directory_disk",)},
            {"wide": {"offset": 5}, "markers": ("offset",)},
            {"wide": {"offset": 5}},
            {"record": False},
            {"locator_offset": 3},
            {"locator_disk": 1},
            {"disks": 2},
            {"record_signature": b"PK\x06\x09"},
            {"record_size": 45},
            {"record_size": 0},
        ],
        ids=lambda options: ",".join(options),
    )
    def test_the_zip64_checks_do_not_rely_on_zipfile_rejecting_the_archive(self, options):
        data = with_zip64_end(self.archive(), **options)
        with pytest.raises(InvalidPackageError) as caught:
            inspector._check_central_directory(data, self.stub())
        assert caught.value.reason == "CORRUPT_ARCHIVE"

    @pytest.mark.parametrize("markers", [(), ("size", "offset"), ("offset",)])
    def test_the_walk_accepts_consistent_zip64_values_whatever_zipfile_thinks(self, markers):
        data = with_zip64_end(self.archive(), markers=markers)
        inspector._check_central_directory(data, self.stub())

    @pytest.mark.parametrize("damage", ["signature", "truncated"])
    def test_a_malformed_record_is_rejected_whatever_zipfile_thinks(self, damage):
        data = self.archive()
        records = central_records(data)
        if damage == "signature":
            second = records[1][0]
            data = data[:second] + b"PK\x09\x09" + data[second + 4 :]
        else:
            end = end_record_offset(data)
            data = data[:end] + bytes(10) + data[end:]
            data = set_end_fields(data, size=end_fields(data)["size"] + 10)
        with pytest.raises(InvalidPackageError) as caught:
            inspector._check_central_directory(data, self.stub())
        assert caught.value.reason == "CORRUPT_ARCHIVE"
        assert "malformed record" in caught.value.message

    @pytest.mark.parametrize("field", ["name", "extra", "comment"])
    def test_the_overflow_cannot_hide_behind_a_zip64_end_record(self, field):
        wrapped = with_zip64_end(self.archive(), markers=("size", "offset"))
        assert reason_of(overflow_central_directory(wrapped, field=field)) == "CORRUPT_ARCHIVE"
        plain = with_zip64_end(self.archive())
        assert reason_of(overflow_central_directory(plain, field=field)) == "CORRUPT_ARCHIVE"

    def test_markers_in_the_end_record_resolve_to_the_real_directory(self):
        plain = self.archive()
        data = with_zip64_end(plain, markers=("size", "offset"))
        assert end_fields(data)["offset"] == 0xFFFFFFFF
        with inspector._open_archive(data, 1 << 20) as archive:
            assert archive.start_dir == end_fields(plain)["offset"]
            inspector._check_central_directory(data, archive)

    def test_a_python_archive_with_a_zip64_end_record_is_accepted(self, monkeypatch):
        with monkeypatch.context() as patch:
            patch.setattr(zipfile, "ZIP_FILECOUNT_LIMIT", 1)
            data = make_zip(LEGIT_FILES, dir_entries=True)
        assert has_zip64_end(data)
        parsed = inspect_package(data)
        assert paths_of(parsed) == sorted(LEGIT_FILES)
        for entry in parsed.manifest:
            content = read_package_file(data, parsed.root_prefix, entry.path)
            assert content == legit_bytes(LEGIT_FILES[entry.path])

    def test_an_empty_archive_has_nothing_to_walk(self):
        data = make_zip({})
        assert end_fields(data)["entries"] == 0
        assert reason_of(data) == "MISSING_ENTRYPOINT"

    @staticmethod
    def tail_mutations(rng, base):
        data = bytearray(base)
        first = last_central_record(base)
        for _ in range(rng.choice((1, 1, 2, 3))):
            position = rng.randrange(first, len(data))
            roll = rng.random()
            if roll < 0.45:
                data[position] ^= 1 << rng.randrange(8)
            elif roll < 0.8:
                data[position] = rng.randrange(256)
            elif roll < 0.9:
                data[position] = 0xFF
            else:
                data[position] = 0
        return bytes(data)

    def test_an_accepted_central_directory_is_read_identically_by_a_length_honouring_reader(self):
        plain = self.archive()
        bases = [
            plain,
            with_zip64_end(plain, markers=("size", "offset")),
            with_zip64_end(plain),
            make_skill_zip(
                "walk-a", "1.0.0", files={"a.txt": "alpha", "b/c.txt": "bravo"}, dir_entries=True
            ),
            make_macos_zip({"SKILL.md": skill_md("walk-b", "1.0.0"), "x/y.txt": "y"}),
        ]
        rng = random.Random(20261017)
        accepted = 0
        for _ in range(4000):
            blob = self.tail_mutations(rng, rng.choice(bases))
            try:
                inspect_package(blob)
            except DomainError:
                continue
            accepted += 1
            with zipfile.ZipFile(io.BytesIO(blob)) as archive:
                infos = archive.infolist()
                expected = [
                    info.orig_filename.encode("utf-8" if info.flag_bits & 0x800 else "cp437")
                    for info in infos
                ]
            assert length_honouring_names(blob, len(infos)) == expected
            assert end_fields(blob)["entries_here"] in (len(infos), 0xFFFF)
        assert accepted >= 100

    def test_info_zip_lists_the_same_names_as_zipfile_for_every_accepted_archive(self, tmp_path):
        unzip = shutil.which("unzip")
        if unzip is None:
            pytest.skip("the unzip command is not installed")
        plain = self.archive()
        bases = [
            plain,
            with_zip64_end(plain, markers=("size", "offset")),
            make_skill_zip(
                "walk-c", "1.0.0", files={"a.txt": "alpha", "b/c.txt": "bravo"}, dir_entries=True
            ),
            make_macos_zip({"SKILL.md": skill_md("walk-d", "1.0.0"), "x/y.txt": "y"}),
        ]
        rng = random.Random(4711)
        compared = 0
        for _ in range(3000):
            blob = self.tail_mutations(rng, rng.choice(bases))
            try:
                inspect_package(blob)
            except DomainError:
                continue
            with zipfile.ZipFile(io.BytesIO(blob)) as archive:
                names = [info.orig_filename for info in archive.infolist()]
                expected = [
                    info.orig_filename.encode("utf-8" if info.flag_bits & 0x800 else "cp437")
                    for info in archive.infolist()
                ]
            if not all(name.isascii() and name.isprintable() for name in names):
                continue
            path = tmp_path / "case.zip"
            path.write_bytes(blob)
            listing = subprocess.run([unzip, "-Z1", str(path)], capture_output=True, check=False)
            assert listing.returncode == 0, listing.stderr
            assert listing.stdout.split(b"\n")[:-1] == expected
            compared += 1
            if compared == 120:
                break
        assert compared == 120

    def test_every_rejected_mutation_of_the_end_region_is_a_domain_error(self):
        rng = random.Random(77)
        bases = [with_zip64_end(self.archive(), markers=("size", "offset")), self.archive()]
        reasons = set()
        for _ in range(3000):
            blob = self.tail_mutations(rng, rng.choice(bases))
            try:
                inspect_package(blob)
            except DomainError as error:
                reasons.add(error.reason)
        assert "CORRUPT_ARCHIVE" in reasons
        assert reasons <= set(PACKAGE_REASONS)


class ScriptedInflater:
    def __init__(self, script):
        self.script = list(script)
        self.eof = False
        self.unconsumed_tail = b""
        self.unused_data = b""

    def decompress(self, data, max_length=0):
        chunk, finished = self.script.pop(0) if self.script else (b"", False)
        self.eof = finished
        return chunk


class ScriptedZlib:
    MAX_WBITS = zlib.MAX_WBITS
    error = zlib.error
    crc32 = staticmethod(zlib.crc32)

    def __init__(self, script):
        self.script = script

    def decompressobj(self, wbits):
        return ScriptedInflater(self.script)


UNREAD_ENTRY_NAMES = ("__MACOSX/._x", ".DS_Store", "assets/Thumbs.db", "docs/", "__MACOSX/docs/")


class TestDeflateExtents:
    CONTENT = "visible content\n" * 4

    @staticmethod
    def hidden_record():
        return layout_local_record(
            layout_stored("../../hidden-by-slack.sh", "#!/bin/sh\necho owned\n")
        )

    @staticmethod
    def package(*entries):
        return build_layout_zip([base_entries()[0], *entries])

    @staticmethod
    def listed_by_zipfile(data):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return archive.namelist()

    @staticmethod
    def unread_entry(name, slack=b"", **overrides):
        if name.endswith("/"):
            overrides.setdefault("external_attr", UNIX_DIRECTORY)
            return deflate_carrier(name, b"", slack, **overrides)
        return deflate_carrier(name, b"junk", slack, **overrides)

    def test_the_control_archive_is_accepted_and_walked_identically(self):
        data = self.package(deflate_carrier("notes.txt", self.CONTENT))
        assert paths_of(inspect_package(data)) == ["SKILL.md", "notes.txt"]
        names, position = deflate_streaming_walk(data)
        assert names == self.listed_by_zipfile(data)
        assert position == central_directory_offset(data)

    @pytest.mark.parametrize("streamed", [False, True], ids=["plain", "descriptor"])
    def test_a_local_header_hidden_after_a_deflate_stream_is_rejected(self, streamed):
        raw = self.CONTENT.encode("utf-8")
        stream = deflate_bytes(raw)
        slack = self.hidden_record()
        if streamed:
            slack = data_descriptor(zlib.crc32(raw), len(stream), len(raw)) + slack
        carrier = deflate_carrier("notes.txt", raw, slack, stream=stream, streamed=streamed)
        data = self.package(carrier)
        assert self.listed_by_zipfile(data) == ["SKILL.md", "notes.txt"]
        names, _ = deflate_streaming_walk(data)
        assert names == ["SKILL.md", "notes.txt", "../../hidden-by-slack.sh"]
        error = rejection(data)
        assert error.reason == "CORRUPT_ARCHIVE"
        assert "notes.txt" in error.message
        assert "follow the end of the deflate stream" in error.message

    @pytest.mark.parametrize(
        "slack",
        [b"\x00", b"JUNK", bytes(4096), b"PK\x03\x04", b"\x03\x00", b"\x01\x00\x00\xff\xff"],
        ids=["one-byte", "text", "page", "signature", "empty-block", "final-empty-block"],
    )
    @pytest.mark.parametrize("streamed", [False, True], ids=["plain", "descriptor"])
    @pytest.mark.parametrize("position", [0, 1, 2])
    def test_any_byte_after_the_end_of_a_stream_is_rejected(self, slack, streamed, position):
        entries = [deflate_carrier(f"f{index}.txt", f"content {index}\n" * 3) for index in range(3)]
        entries[position] = deflate_carrier("carrier.txt", self.CONTENT, slack, streamed=streamed)
        error = rejection(self.package(*entries))
        assert error.reason == "CORRUPT_ARCHIVE"
        assert "carrier.txt" in error.message

    def test_a_stream_without_its_final_block_is_rejected(self):
        stream = deflate_bytes(self.CONTENT, final=False)
        data = self.package(deflate_carrier("notes.txt", self.CONTENT, stream=stream))
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            assert archive.read("notes.txt") == self.CONTENT.encode("utf-8")
        assert deflate_streaming_walk(data) is None
        error = rejection(data)
        assert error.reason == "CORRUPT_ARCHIVE"
        assert "not terminated" in error.message

    def test_a_stream_cut_short_by_its_declared_size_is_rejected(self):
        stream = deflate_bytes(self.CONTENT)
        carrier = deflate_carrier("notes.txt", self.CONTENT, compressed_size=len(stream) - 1)
        assert reason_of(self.package(carrier)) == "CORRUPT_ARCHIVE"

    @staticmethod
    def stored_blocks_of(size):
        return bytes(range(256)) * (size // 256) + bytes(range(size % 256))

    @pytest.mark.parametrize(
        "shape",
        ["level-0", "level-1", "level-6", "level-9", "segments", "empty", "large", "stored-large"],
    )
    def test_every_complete_stream_is_accepted(self, shape):
        content = self.stored_blocks_of(300_000) if "large" in shape else b"hello " * 100
        if shape == "empty":
            content = b""
        if shape == "segments":
            compressor = zlib.compressobj(6, zlib.DEFLATED, -15)
            stream = (
                compressor.compress(content[:100])
                + compressor.flush(zlib.Z_SYNC_FLUSH)
                + compressor.compress(content[100:])
                + compressor.flush(zlib.Z_FINISH)
            )
        else:
            level = {"level-0": 0, "level-1": 1, "level-6": 6, "stored-large": 0}.get(shape, 9)
            stream = deflate_bytes(content, level=level)
        data = self.package(deflate_carrier("f.bin", content, stream=stream))
        parsed = inspect_package(data)
        assert manifest_of(parsed)["f.bin"].size == len(content)
        assert read_package_file(data, "", "f.bin") == content
        names, _ = deflate_streaming_walk(data)
        assert names == self.listed_by_zipfile(data)

    @classmethod
    def stored_stream_of(cls, target):
        for size in range(target - 30, target):
            content = cls.stored_blocks_of(size)
            stream = deflate_bytes(content, level=0)
            if len(stream) == target:
                return content, stream
        raise AssertionError(f"no stored stream of {target} bytes")

    @pytest.mark.parametrize("target", [65_535, 65_536, 65_537, 131_072, 131_073])
    def test_streams_ending_on_a_chunk_boundary_are_exact(self, target):
        content, stream = self.stored_stream_of(target)
        data = self.package(deflate_carrier("f.bin", content, stream=stream))
        assert manifest_of(inspect_package(data))["f.bin"].size == len(content)
        assert read_package_file(data, "", "f.bin") == content
        padded = self.package(deflate_carrier("f.bin", content, b"\x00", stream=stream))
        assert reason_of(padded) == "CORRUPT_ARCHIVE"
        cut = self.package(deflate_carrier("f.bin", content, stream=stream[:-1]))
        assert reason_of(cut) == "CORRUPT_ARCHIVE"

    @pytest.mark.parametrize("name", ["f.bin", *UNREAD_ENTRY_NAMES])
    def test_the_patched_data_flag_is_refused_wherever_it_appears(self, name):
        entry = self.unread_entry(name, flags=0x20)
        error = rejection(self.package(entry))
        assert error.reason == "CORRUPT_ARCHIVE"
        assert "patched data" in error.message

    @staticmethod
    def entry_of(data, name):
        archive = inspector._open_archive(data, 1 << 20)
        headers = inspector._check_tessellation(data, archive)
        info = archive.getinfo(name)
        return info, headers[info.header_offset]

    def test_output_still_pending_after_the_last_input_byte_is_collected(self, monkeypatch):
        data = self.package(deflate_carrier("notes.txt", self.CONTENT))
        info, header = self.entry_of(data, "notes.txt")
        script = [(b"a" * 65_536, False), (b"b" * 100, True)]
        monkeypatch.setattr(inspector, "zlib", ScriptedZlib(script))

        chunks = list(inspector._deflated_chunks(data, info, header, check_crc=False))

        assert chunks == [b"a" * 65_536, b"b" * 100]

    def test_a_stream_that_runs_dry_without_ending_is_not_terminated(self, monkeypatch):
        data = self.package(deflate_carrier("notes.txt", self.CONTENT))
        info, header = self.entry_of(data, "notes.txt")
        monkeypatch.setattr(inspector, "zlib", ScriptedZlib([(b"a" * 10, False)]))

        with pytest.raises(zipfile.BadZipFile, match="not terminated"):
            list(inspector._deflated_chunks(data, info, header, check_crc=False))

    def test_a_deflate_entry_with_no_stream_at_all_is_an_empty_file(self):
        empty = LayoutEntry("empty.txt", b"", method=zipfile.ZIP_DEFLATED, crc=0)
        assert manifest_of(inspect_package(self.package(empty)))["empty.txt"].size == 0
        lying = LayoutEntry(
            "empty.txt", b"", method=zipfile.ZIP_DEFLATED, crc=0, uncompressed_size=5
        )
        assert reason_of(self.package(lying)) == "CORRUPT_ARCHIVE"

    @pytest.mark.parametrize("name", UNREAD_ENTRY_NAMES)
    def test_valid_streams_of_entries_that_are_never_read_are_accepted(self, name):
        data = self.package(self.unread_entry(name))
        assert paths_of(inspect_package(data)) == ["SKILL.md"]
        names, _ = deflate_streaming_walk(data)
        assert names == self.listed_by_zipfile(data)

    @pytest.mark.parametrize("name", UNREAD_ENTRY_NAMES)
    @pytest.mark.parametrize("streamed", [False, True], ids=["plain", "descriptor"])
    def test_a_hidden_header_after_the_stream_of_an_entry_that_is_never_read_is_rejected(
        self, name, streamed
    ):
        raw = b"" if name.endswith("/") else b"junk"
        stream = deflate_bytes(raw)
        slack = self.hidden_record()
        if streamed:
            slack = data_descriptor(zlib.crc32(raw), len(stream), len(raw)) + slack
        data = self.package(self.unread_entry(name, slack, streamed=streamed))
        names, _ = deflate_streaming_walk(data)
        assert names[-1] == "../../hidden-by-slack.sh"
        error = rejection(data)
        assert error.reason == "CORRUPT_ARCHIVE"
        assert name in error.message

    @pytest.mark.parametrize("name", UNREAD_ENTRY_NAMES)
    def test_an_unterminated_stream_of_an_entry_that_is_never_read_is_rejected(self, name):
        raw = b"" if name.endswith("/") else b"junk"
        entry = self.unread_entry(name, stream=deflate_bytes(raw, final=False))
        error = rejection(self.package(entry))
        assert error.reason == "CORRUPT_ARCHIVE"
        assert "not terminated" in error.message

    @pytest.mark.parametrize("name", ["docs/", "__MACOSX/docs/"])
    def test_a_directory_whose_stream_inflates_to_content_is_rejected(self, name):
        entry = self.unread_entry(name, stream=deflate_bytes(b"hidden payload"))
        error = rejection(self.package(entry))
        assert error.reason == "CORRUPT_ARCHIVE"
        assert "has content" in error.message

    def test_a_stored_block_that_reaches_into_the_next_entry_cannot_desynchronise_a_reader(self):
        filler = b"-" * 20
        victim_data = filler + b"\x01\x00\x00\xff\xff" + self.hidden_record()
        reach = 30 + len(b"a.txt") + len(filler)
        block = b"\x00" + struct.pack("<HH", reach, reach ^ 0xFFFF)
        junk = LayoutEntry(
            "__MACOSX/._x", block, method=zipfile.ZIP_DEFLATED, uncompressed_size=reach
        )
        data = self.package(junk, layout_stored("a.txt", victim_data))
        assert self.listed_by_zipfile(data) == ["SKILL.md", "__MACOSX/._x", "a.txt"]
        names, _ = deflate_streaming_walk(data)
        assert names == ["SKILL.md", "__MACOSX/._x", "../../hidden-by-slack.sh"]
        error = rejection(data)
        assert error.reason == "CORRUPT_ARCHIVE"
        assert "__MACOSX/._x" in error.message

    def test_data_after_a_stream_makes_the_entry_unreadable_from_the_stored_blob(self):
        data = self.package(deflate_carrier("notes.txt", self.CONTENT, self.hidden_record()))
        assert read_package_file(data, "", "SKILL.md")
        with pytest.raises(InvalidPackageError) as caught:
            read_package_file(data, "", "notes.txt")
        assert caught.value.reason == "CORRUPT_ARCHIVE"
        assert "follow the end of the deflate stream" in caught.value.message

    @pytest.mark.parametrize(
        "overrides",
        [
            {"local_name": "different.txt"},
            {"local_compressed_size": 3},
            {"local_uncompressed_size": 3},
            {"local_crc": 1},
            {"local_method": zipfile.ZIP_STORED},
        ],
        ids=lambda overrides: ",".join(overrides),
    )
    def test_a_deflate_entry_with_a_wrong_local_header_is_refused_on_read(self, overrides):
        entry = deflate_carrier("harmless.txt", self.CONTENT, **overrides)
        data = self.package(entry)
        assert read_package_file(
            self.package(deflate_carrier("harmless.txt", self.CONTENT)), "", "harmless.txt"
        )
        with pytest.raises(InvalidPackageError) as caught:
            read_package_file(data, "", "harmless.txt")
        assert caught.value.reason == "CORRUPT_ARCHIVE"
        assert "local header" in caught.value.message

    def test_a_deflate_entry_whose_data_lies_beyond_the_archive_is_refused(self):
        data = self.package(deflate_carrier("notes.txt", self.CONTENT))
        forged = forge_compressed_size(data, "notes.txt", 5_000_000)
        assert reason_of(forged) == "CORRUPT_ARCHIVE"
        with pytest.raises(InvalidPackageError) as caught:
            read_package_file(forged, "", "notes.txt")
        assert caught.value.reason == "CORRUPT_ARCHIVE"
        assert "beyond the end" in caught.value.message

    def test_a_stored_blob_with_a_wrong_local_header_is_refused_on_read(self):
        data = build_raw_zip(
            [
                stored_entry("SKILL.md", skill_md("liar", "1.0.0")),
                stored_entry("harmless.txt", "payload", local_name="different.txt"),
            ]
        )
        with pytest.raises(InvalidPackageError) as caught:
            read_package_file(data, "", "harmless.txt")
        assert caught.value.reason == "CORRUPT_ARCHIVE"

    @staticmethod
    def stream_spans(data):
        spans = []
        for central, _, local in central_records(data):
            compressed = struct.unpack_from("<I", data, central + 20)[0]
            name_length, extra_length = struct.unpack_from("<HH", data, local + 26)
            start = local + 30 + name_length + extra_length
            spans.append(
                (max(start, start + compressed - 6), min(len(data), start + compressed + 26))
            )
        return spans

    def test_an_accepted_archive_is_walked_identically_by_a_deflate_aware_reader(self):
        limits = PackageLimits(max_package_bytes=1 * MIB, max_files=200)
        files = {"scripts/run.py": "print(1)\n" * 40, "data.bin": bytes(range(256)) * 4}
        bases = [
            make_skill_zip("flate-a", "1.0.0", files=files, dir_entries=True, macos_junk=True),
            make_macos_zip({"SKILL.md": skill_md("flate-b", "1.0.0"), **files}, wrapper="pkg"),
            make_stream_zip({"SKILL.md": skill_md("flate-c", "1.0.0"), **files}, dir_entries=True),
            make_skill_zip("flate-d", "1.0.0", files=files, wrapper="pkg"),
        ]
        spans = [self.stream_spans(base) + header_spans(base) for base in bases]
        rng = random.Random(20261018)
        compared = accepted = 0
        for _ in range(4000):
            index = rng.randrange(len(bases))
            blob = bytearray(bases[index])
            for _ in range(rng.choice((1, 1, 2, 3))):
                start, stop = rng.choice(spans[index])
                position = rng.randrange(start, max(start + 1, min(stop, len(blob))))
                roll = rng.random()
                if roll < 0.5:
                    blob[position] ^= 1 << rng.randrange(8)
                elif roll < 0.85:
                    blob[position] = rng.randrange(256)
                else:
                    blob[position] = rng.choice((0, 0xFF))
            blob = bytes(blob)
            try:
                inspect_package(blob, limits)
            except DomainError:
                continue
            accepted += 1
            walked = deflate_streaming_walk(blob)
            if walked is None:
                continue
            compared += 1
            names, position = walked
            with zipfile.ZipFile(io.BytesIO(blob)) as archive:
                listed = [
                    info.orig_filename
                    for info in sorted(archive.infolist(), key=lambda info: info.header_offset)
                ]
                assert names == listed
                assert position == archive.start_dir
        assert accepted >= 200
        assert compared >= 100


LEGIT_FILES = {
    "SKILL.md": skill_md("legit-package", "1.0.0"),
    "scripts/run.py": "print('legit')\n",
    "docs/readme.md": "# Readme\n",
    "data/blob.bin": bytes(range(256)) * 20,
    "empty.txt": "",
}
LEGIT_WRAPPER = "legit-folder"


def legit_bytes(content):
    return content if isinstance(content, bytes) else content.encode("utf-8")


def descriptor_package(*, signature, zip64):
    entries = []
    for name, content in LEGIT_FILES.items():
        raw = legit_bytes(content)
        descriptor = data_descriptor(
            zlib.crc32(raw), len(raw), len(raw), signature=signature, zip64=zip64
        )
        entries.append(layout_stored(name, raw, streamed=True, descriptor=descriptor))
    return build_layout_zip(entries)


def deflated_descriptor_package():
    entries = [
        layout_deflated(name, legit_bytes(content), streamed=True)
        for name, content in LEGIT_FILES.items()
    ]
    return build_layout_zip(entries, comment=b"archive comment")


def split_extra_package():
    entries = []
    for name, content in LEGIT_FILES.items():
        entries.append(
            layout_deflated(
                name,
                legit_bytes(content),
                local_extra=struct.pack("<HHBII", 0x5455, 9, 3, 1_600_000_000, 1_600_000_000)
                + struct.pack("<HHBBIBI", 0x7875, 11, 1, 4, 1000, 4, 1000),
                central_extra=struct.pack("<HHBI", 0x5455, 5, 3, 1_600_000_000)
                + struct.pack("<HHBBIBI", 0x7875, 11, 1, 4, 1000, 4, 1000),
            )
        )
    return build_layout_zip(entries)


def zip64_end_package():
    original = zipfile.ZIP_FILECOUNT_LIMIT
    zipfile.ZIP_FILECOUNT_LIMIT = 1
    try:
        return make_zip(LEGIT_FILES, dir_entries=True)
    finally:
        zipfile.ZIP_FILECOUNT_LIMIT = original


ARCHIVE_BUILDERS = {
    "python-deflate": (lambda: make_zip(LEGIT_FILES), ""),
    "python-stored": (lambda: make_zip(LEGIT_FILES, compression=zipfile.ZIP_STORED), ""),
    "python-directories": (lambda: make_zip(LEGIT_FILES, dir_entries=True), ""),
    "python-wrapper": (
        lambda: make_zip(LEGIT_FILES, wrapper=LEGIT_WRAPPER, dir_entries=True),
        LEGIT_WRAPPER + "/",
    ),
    "python-junk": (
        lambda: make_zip(LEGIT_FILES, wrapper=LEGIT_WRAPPER, dir_entries=True, macos_junk=True),
        LEGIT_WRAPPER + "/",
    ),
    "python-stream-stored": (
        lambda: make_stream_zip(LEGIT_FILES, compression=zipfile.ZIP_STORED),
        "",
    ),
    "python-stream-deflate": (lambda: make_stream_zip(LEGIT_FILES), ""),
    "python-stream-directories": (lambda: make_stream_zip(LEGIT_FILES, dir_entries=True), ""),
    "python-stream-junk": (
        lambda: make_stream_zip(
            LEGIT_FILES, wrapper=LEGIT_WRAPPER, dir_entries=True, macos_junk=True
        ),
        LEGIT_WRAPPER + "/",
    ),
    "python-stream-comment": (lambda: make_stream_zip(LEGIT_FILES, comment=b"made by hand"), ""),
    "python-zip64": (lambda: make_zip64_zip(LEGIT_FILES), ""),
    "python-zip64-stored": (
        lambda: make_zip64_zip(LEGIT_FILES, compression=zipfile.ZIP_STORED, dir_entries=True),
        "",
    ),
    "python-stream-zip64": (lambda: make_stream_zip(LEGIT_FILES, force_zip64=True), ""),
    "python-stream-zip64-stored": (
        lambda: make_stream_zip(LEGIT_FILES, force_zip64=True, compression=zipfile.ZIP_STORED),
        "",
    ),
    "python-zip64-end-record": (zip64_end_package, ""),
    "macos-archive-utility": (
        lambda: make_macos_zip(LEGIT_FILES, wrapper=LEGIT_WRAPPER),
        LEGIT_WRAPPER + "/",
    ),
    "zipalign-4": (lambda: make_zipalign_zip(LEGIT_FILES), ""),
    "zipalign-4096": (lambda: make_zipalign_zip(LEGIT_FILES, alignment=4096), ""),
    "descriptor-with-signature": (lambda: descriptor_package(signature=True, zip64=False), ""),
    "descriptor-without-signature": (lambda: descriptor_package(signature=False, zip64=False), ""),
    "descriptor-zip64": (lambda: descriptor_package(signature=True, zip64=True), ""),
    "descriptor-zip64-without-signature": (
        lambda: descriptor_package(signature=False, zip64=True),
        "",
    ),
    "descriptor-deflated-with-comment": (deflated_descriptor_package, ""),
    "split-extra-fields": (split_extra_package, ""),
}


def info_zip(*arguments, cwd, stdin=None):
    zip_binary = shutil.which("zip")
    if zip_binary is None:
        pytest.skip("the zip command is not installed")
    result = subprocess.run(
        [zip_binary, *arguments], cwd=cwd, input=stdin, capture_output=True, check=False
    )
    if result.returncode != 0:
        pytest.skip(f"this zip build rejected the arguments: {result.stderr[:80]!r}")
    return result.stdout


def write_tree(root, files):
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(legit_bytes(content))


class TestLegitimateArchives:
    @pytest.mark.parametrize("label", list(ARCHIVE_BUILDERS))
    def test_archives_from_common_writers_are_accepted(self, label):
        build, prefix = ARCHIVE_BUILDERS[label]
        data = build()
        parsed = inspect_package(data)
        assert parsed.name == "legit-package"
        assert parsed.root_prefix == prefix
        assert paths_of(parsed) == sorted(LEGIT_FILES)
        for entry in parsed.manifest:
            content = read_package_file(data, parsed.root_prefix, entry.path)
            assert content == legit_bytes(LEGIT_FILES[entry.path])
            assert sha(content) == entry.sha256

    def test_the_stream_fixtures_really_carry_data_descriptors(self):
        for label in ("python-stream-stored", "python-stream-deflate", "python-stream-zip64"):
            data = ARCHIVE_BUILDERS[label][0]()
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                flagged = [i for i in archive.infolist() if i.flag_bits & DESCRIPTOR_FLAG]
            assert len(flagged) == len(LEGIT_FILES)

    def test_the_zip64_end_fixture_really_has_a_zip64_end_record(self):
        assert has_zip64_end(ARCHIVE_BUILDERS["python-zip64-end-record"][0]())
        assert not has_zip64_end(ARCHIVE_BUILDERS["python-deflate"][0]())

    def test_the_zip64_fixtures_really_use_zip64_sentinels(self):
        data = ARCHIVE_BUILDERS["python-zip64"][0]()
        offset = local_header_offset(data, "scripts/run.py")
        assert struct.unpack_from("<II", data, offset + 18) == (ZIP64_MARKER, ZIP64_MARKER)
        streamed = ARCHIVE_BUILDERS["python-stream-zip64"][0]()
        assert len(streamed) > len(ARCHIVE_BUILDERS["python-stream-deflate"][0]())

    def test_the_zipalign_fixtures_really_pad_the_local_extra_field(self):
        data = ARCHIVE_BUILDERS["zipalign-4096"][0]()
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for info in archive.infolist():
                local = local_header_offset(data, info.orig_filename)
                name_length, extra_length = struct.unpack_from("<HH", data, local + 26)
                assert (local + 30 + name_length + extra_length) % 4096 == 0
                assert info.extra == b""
        assert any(
            struct.unpack_from("<H", data, local_header_offset(data, name) + 28)[0] > 0
            for name in LEGIT_FILES
        )

    @pytest.mark.parametrize(
        "options",
        [
            ["-r"],
            ["-r", "-D"],
            ["-r", "-0"],
            ["-r", "-9"],
            ["-r", "-X"],
            ["-r", "-fz"],
            ["-r", "-fz", "-0"],
        ],
        ids=[
            "recursive",
            "no-directory-entries",
            "stored",
            "best",
            "no-attributes",
            "zip64",
            "zip64-stored",
        ],
    )
    @pytest.mark.parametrize("layout", ["wrapped", "flat"])
    def test_archives_written_by_info_zip_are_accepted(self, tmp_path, options, layout):
        source = tmp_path / LEGIT_WRAPPER
        write_tree(source, LEGIT_FILES)
        archive = tmp_path / "out.zip"
        target = LEGIT_WRAPPER if layout == "wrapped" else "."
        cwd = tmp_path if layout == "wrapped" else source
        info_zip(*options, "-q", str(archive), target, cwd=cwd)
        data = archive.read_bytes()
        assert has_zip64_end(data) == ("-fz" in options)
        parsed = inspect_package(data)
        assert parsed.root_prefix == (LEGIT_WRAPPER + "/" if layout == "wrapped" else "")
        assert paths_of(parsed) == sorted(LEGIT_FILES)
        for entry in parsed.manifest:
            content = read_package_file(data, parsed.root_prefix, entry.path)
            assert content == legit_bytes(LEGIT_FILES[entry.path])

    def test_a_streamed_archive_written_by_info_zip_is_accepted(self, tmp_path):
        write_tree(tmp_path / LEGIT_WRAPPER, LEGIT_FILES)
        data = info_zip("-r", "-q", "-", LEGIT_WRAPPER, cwd=tmp_path)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            assert any(info.flag_bits & DESCRIPTOR_FLAG for info in archive.infolist())
        parsed = inspect_package(data)
        assert parsed.root_prefix == LEGIT_WRAPPER + "/"
        assert paths_of(parsed) == sorted(LEGIT_FILES)

    def test_an_archive_comment_written_by_info_zip_is_accepted(self, tmp_path):
        write_tree(tmp_path / LEGIT_WRAPPER, LEGIT_FILES)
        archive = tmp_path / "out.zip"
        info_zip("-r", "-q", str(archive), LEGIT_WRAPPER, cwd=tmp_path)
        info_zip("-z", "-q", str(archive), cwd=tmp_path, stdin=b"a comment\n")
        with zipfile.ZipFile(archive) as opened:
            assert opened.comment.startswith(b"a comment")
        assert inspect_package(archive.read_bytes()).root_prefix == LEGIT_WRAPPER + "/"

    def test_an_archive_updated_in_place_by_info_zip_is_accepted(self, tmp_path):
        write_tree(tmp_path / LEGIT_WRAPPER, LEGIT_FILES)
        archive = tmp_path / "out.zip"
        info_zip(
            "-q",
            str(archive),
            f"{LEGIT_WRAPPER}/SKILL.md",
            f"{LEGIT_WRAPPER}/empty.txt",
            cwd=tmp_path,
        )
        info_zip(
            "-q",
            str(archive),
            f"{LEGIT_WRAPPER}/scripts/run.py",
            f"{LEGIT_WRAPPER}/docs/readme.md",
            cwd=tmp_path,
        )
        info_zip("-g", "-q", str(archive), f"{LEGIT_WRAPPER}/data/blob.bin", cwd=tmp_path)
        assert paths_of(inspect_package(archive.read_bytes())) == sorted(LEGIT_FILES)

    PERL_WRITER = r"""
use strict;
use warnings;
use File::Find;
use IO::Compress::Zip qw($ZipError :zip_method);
my ($root, $mode, $out) = @ARGV;
my @files;
find({ no_chdir => 1, wanted => sub { push @files, $File::Find::name if -f $_ } }, $root);
my $target = $out;
if ($mode =~ /stream/) { $target = \*STDOUT; binmode STDOUT; }
my $first = 1;
my $zip;
for my $path (sort @files) {
    my $name = $path;
    open(my $handle, "<:raw", $path) or die;
    local $/;
    my $content = <$handle>;
    close $handle;
    my %options = (
        Name => $name,
        Method => ZIP_CM_DEFLATE,
        Stream => ($mode =~ /stream/ ? 1 : 0),
        Zip64 => ($mode =~ /zip64/ ? 1 : 0),
        CanonicalName => 1,
        ExtAttr => (0100644 << 16),
    );
    if ($first) { $zip = IO::Compress::Zip->new($target, %options) or die $ZipError; $first = 0; }
    else { $zip->newStream(%options) or die $ZipError; }
    $zip->print($content);
}
$zip->close();
"""

    @pytest.mark.parametrize("mode", ["plain", "zip64", "stream", "stream-zip64"])
    def test_archives_written_by_perl_are_accepted(self, tmp_path, mode):
        perl = shutil.which("perl")
        if perl is None:
            pytest.skip("perl is not installed")
        probe = subprocess.run(
            [perl, "-MIO::Compress::Zip", "-e1"], capture_output=True, check=False
        )
        if probe.returncode != 0:
            pytest.skip("IO::Compress::Zip is not installed")
        write_tree(tmp_path / LEGIT_WRAPPER, LEGIT_FILES)
        archive = tmp_path / "out.zip"
        run = subprocess.run(
            [perl, "-e", self.PERL_WRITER, LEGIT_WRAPPER, mode, str(archive)],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )
        data = run.stdout if "stream" in mode else archive.read_bytes()
        assert has_zip64_end(data) == ("zip64" in mode)
        with zipfile.ZipFile(io.BytesIO(data)) as opened:
            flagged = [i for i in opened.infolist() if i.flag_bits & DESCRIPTOR_FLAG]
        assert bool(flagged) == ("stream" in mode)
        parsed = inspect_package(data)
        assert parsed.root_prefix == LEGIT_WRAPPER + "/"
        assert paths_of(parsed) == sorted(LEGIT_FILES)


class CountingStruct:
    def __init__(self, wrapped):
        self.wrapped = wrapped
        self.size = wrapped.size
        self.calls = 0

    def unpack_from(self, buffer, offset=0):
        self.calls += 1
        return self.wrapped.unpack_from(buffer, offset)


def ceiling_package(regular, ignored):
    entries = [layout_stored("SKILL.md", skill_md("ceiling", "1.0.0"))]
    entries += [layout_stored(f"d{index % 40}/f{index}.txt", "x") for index in range(regular)]
    entries += [
        layout_stored(f"__MACOSX/d{index % 40}/._f{index}", "x") for index in range(ignored)
    ]
    return build_layout_zip(entries)


def best_cpu_time(function, repeats=7):
    best = float("inf")
    for _ in range(repeats):
        started = time.process_time()
        function()
        best = min(best, time.process_time() - started)
    return best


class TestTessellationPerformance:
    def test_each_local_header_is_read_exactly_once(self, monkeypatch):
        data = ceiling_package(1500, 3000)
        counter = CountingStruct(inspector._LOCAL_HEADER)
        monkeypatch.setattr(inspector, "_LOCAL_HEADER", counter)
        parsed = inspect_package(data)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = len(archive.infolist())
        assert entries == 4501 and parsed.file_count == 1501
        assert counter.calls == entries

    def test_the_cost_per_entry_does_not_grow_with_the_number_of_entries(self):
        def measure(ignored):
            data = ceiling_package(100, ignored)
            archive = inspector._open_archive(data, 1 << 30)
            entries = len(archive.infolist())
            elapsed = best_cpu_time(lambda: inspector._check_tessellation(data, archive))
            return entries, elapsed

        small_entries, small = measure(900)
        large_entries, large = measure(7800)
        assert large_entries > 7 * small_entries
        assert large / large_entries < 3 * (small / small_entries)

    def test_an_archive_at_the_entry_ceiling_is_inspected_in_bounded_time(self):
        data = ceiling_package(1999, 6000)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            assert len(archive.infolist()) == 8000
        started = time.process_time()
        parsed = inspect_package(data)
        assert time.process_time() - started < 5
        assert parsed.file_count == 2000

    def test_a_hostile_archive_is_refused_without_walking_the_whole_directory(self, monkeypatch):
        entries = [layout_stored("SKILL.md", skill_md("early", "1.0.0"), gap_after=b"JUNK")]
        entries += [layout_stored(f"f{index}.txt", "x") for index in range(1500)]
        counter = CountingStruct(inspector._LOCAL_HEADER)
        monkeypatch.setattr(inspector, "_LOCAL_HEADER", counter)
        assert reason_of(build_layout_zip(entries)) == "CORRUPT_ARCHIVE"
        assert counter.calls == 1


def header_spans(data):
    spans = []
    for central, name, local in central_records(data):
        name_length, extra_length, comment_length = struct.unpack_from("<HHH", data, central + 28)
        spans.append((central, central + 46 + name_length + extra_length + comment_length))
        local_name, local_extra = struct.unpack_from("<HH", data, local + 26)
        spans.append((local, local + 30 + local_name + local_extra))
    end = data.rfind(b"PK\x05\x06")
    spans.append((end, end + 22))
    return spans


def local_compressed_size(data, position, compressed, extra_start, extra_length):
    if compressed != ZIP64_MARKER:
        return compressed
    cursor, end = extra_start, extra_start + extra_length
    while cursor + 4 <= end:
        tag, length = struct.unpack_from("<HH", data, cursor)
        if tag == 1 and length >= 16:
            return struct.unpack_from("<QQ", data, cursor + 4)[1]
        cursor += 4 + length
    return compressed


def sequential_walk(data):
    names = []
    position = 0
    while data[position : position + 4] == b"PK\x03\x04":
        fields = struct.unpack_from("<IHHHHHIIIHH", data, position)
        flags, compressed, name_length, extra_length = fields[2], fields[7], fields[9], fields[10]
        if flags & DESCRIPTOR_FLAG:
            return None
        raw = data[position + 30 : position + 30 + name_length]
        names.append(raw.decode("utf-8" if flags & 0x800 else "cp437"))
        extra_start = position + 30 + name_length
        compressed = local_compressed_size(data, position, compressed, extra_start, extra_length)
        position = extra_start + extra_length + compressed
    return names, position


class TestTessellationFuzzing:
    def mutated(self, rng, base, spans):
        data = bytearray(base)
        for _ in range(rng.choice((1, 1, 1, 2, 3))):
            start, stop = rng.choice(spans)
            position = rng.randrange(start, min(stop, len(data)))
            roll = rng.random()
            if roll < 0.5:
                data[position] ^= 1 << rng.randrange(8)
            elif roll < 0.8:
                data[position] = rng.randrange(256)
            elif roll < 0.9:
                data[position] = 0xFF
            else:
                data[position] = 0
        return bytes(data)

    def test_header_mutations_only_ever_raise_domain_errors(self):
        limits = PackageLimits(
            max_package_bytes=1 * MIB, max_uncompressed_bytes=4 * MIB, max_file_bytes=2 * MIB
        )
        files = {"scripts/run.py": "print(1)\n" * 20, "data.bin": bytes(range(256)) * 4}
        bases = [
            make_macos_zip({"SKILL.md": skill_md("fuzz-a", "1.0.0"), **files}, wrapper="pkg"),
            make_stream_zip(
                {"SKILL.md": skill_md("fuzz-b", "1.0.0"), **files},
                force_zip64=True,
                dir_entries=True,
                macos_junk=True,
            ),
            make_zipalign_zip({"SKILL.md": skill_md("fuzz-c", "1.0.0"), **files}),
            make_zip64_zip({"SKILL.md": skill_md("fuzz-d", "1.0.0"), **files}, wrapper="pkg"),
            make_skill_zip(
                "fuzz-e", "1.0.0", files=files, dir_entries=True, macos_junk=True, wrapper="pkg"
            ),
        ]
        spans = [header_spans(base) for base in bases]
        rng = random.Random(20261016)
        outcomes = set()
        for _ in range(2500):
            index = rng.randrange(len(bases))
            blob = self.mutated(rng, bases[index], spans[index])
            try:
                inspect_package(blob, limits)
                outcomes.add("ok")
            except DomainError as error:
                outcomes.add(error.reason)
            try:
                read_package_file(blob, "pkg/", "scripts/run.py", max_bytes=1000)
            except DomainError:
                pass
        assert "ok" in outcomes
        assert "CORRUPT_ARCHIVE" in outcomes
        assert len(outcomes) >= 4
        assert outcomes <= set(PACKAGE_REASONS) | {"ok"}

    def test_an_accepted_archive_is_walked_identically_by_a_streaming_reader(self):
        limits = PackageLimits(max_package_bytes=1 * MIB, max_files=200)
        files = {"scripts/run.py": "print(1)\n" * 20, "data.bin": bytes(range(256)) * 4}
        bases = [
            make_skill_zip("walk-a", "1.0.0", files=files, dir_entries=True, macos_junk=True),
            make_skill_zip(
                "walk-b",
                "1.0.0",
                files=files,
                compression=zipfile.ZIP_STORED,
                wrapper="pkg",
                dir_entries=True,
            ),
            make_zipalign_zip({"SKILL.md": skill_md("walk-c", "1.0.0"), **files}),
            make_zip64_zip({"SKILL.md": skill_md("walk-d", "1.0.0"), **files}),
        ]
        spans = [header_spans(base) for base in bases]
        rng = random.Random(77)
        compared = 0
        for _ in range(3500):
            index = rng.randrange(len(bases))
            blob = self.mutated(rng, bases[index], spans[index])
            try:
                inspect_package(blob, limits)
            except DomainError:
                continue
            walked = sequential_walk(blob)
            if walked is None:
                continue
            compared += 1
            names, position = walked
            with zipfile.ZipFile(io.BytesIO(blob)) as archive:
                listed = [
                    info.orig_filename
                    for info in sorted(archive.infolist(), key=lambda info: info.header_offset)
                ]
                assert names == listed
                assert position == archive.start_dir
        assert compared >= 100


class TestCentralDirectoryFlood:
    def test_a_huge_central_directory_is_refused_before_it_is_parsed(self):
        data = make_directory_flood_zip(200_000)
        assert len(data) < DEFAULT_LIMITS.max_package_bytes
        tracemalloc.start()
        started = time.perf_counter()
        try:
            error = rejection(data)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert error.reason == "TOO_MANY_FILES"
        assert time.perf_counter() - started < 1
        assert peak < 8 * MIB

    def test_a_flood_below_the_central_directory_cap_is_still_refused(self):
        data = make_directory_flood_zip(3000)
        assert len(data) < 2 * MIB
        assert reason_of(data) == "CORRUPT_ARCHIVE"

    def test_a_flood_of_distinct_files_below_the_cap_is_stopped_by_the_file_count(self):
        entries = [stored_entry(f"f{index}.txt", "x") for index in range(3000)]
        data = build_raw_zip([stored_entry("SKILL.md", skill_md("flood", "1.0.0")), *entries])
        assert reason_of(data) == "TOO_MANY_FILES"

    def test_the_flood_is_refused_for_small_limits_as_well(self):
        data = make_directory_flood_zip(30_000)
        assert reason_of(data, PackageLimits(max_files=3)) == "TOO_MANY_FILES"

    def test_a_package_with_many_legitimate_entries_still_passes_the_directory_guard(self):
        files = {f"dir{index % 30}/sub/file-{index}.txt": "x" for index in range(1990)}
        data = make_skill_zip(files=files, dir_entries=True, macos_junk=True)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            assert len(archive.namelist()) > 4000
        assert inspect_package(data).file_count == 1991


class TestLimits:
    def test_package_size_limit_boundaries(self):
        data = make_skill_zip(files={"a.bin": bytes(range(256)) * 50})
        assert inspect_package(data, PackageLimits(max_package_bytes=len(data))).file_count == 2
        error = rejection(data, PackageLimits(max_package_bytes=len(data) - 1))
        assert error.reason == "TOO_LARGE"
        assert str(len(data) - 1) in error.message

    def test_the_package_size_is_checked_before_the_archive_is_parsed(self):
        assert reason_of(b"x" * 2000, PackageLimits(max_package_bytes=1000)) == "TOO_LARGE"
        assert reason_of(b"x" * 2000) == "NOT_A_ZIP"

    def test_file_size_limit_boundaries(self):
        content = bytes(range(256)) * 4
        data = make_skill_zip(files={"a.bin": content})
        entrypoint_size = len(skill_md().encode("utf-8"))
        limit = max(len(content), entrypoint_size)
        assert inspect_package(data, PackageLimits(max_file_bytes=limit)).file_count == 2
        error = rejection(data, PackageLimits(max_file_bytes=len(content) - 1))
        assert error.reason == "TOO_LARGE"
        assert "a.bin" in error.message

    def test_the_entrypoint_is_subject_to_the_file_size_limit(self):
        data = make_skill_zip(body="b" * 5000)
        assert reason_of(data, PackageLimits(max_file_bytes=4000)) == "TOO_LARGE"

    def test_total_uncompressed_limit_boundaries(self):
        files = {f"f{index}.bin": bytes([index]) * 1000 for index in range(5)}
        data = make_skill_zip(files=files)
        total = inspect_package(data).uncompressed_bytes
        assert inspect_package(data, PackageLimits(max_uncompressed_bytes=total)).file_count == 6
        error = rejection(data, PackageLimits(max_uncompressed_bytes=total - 1))
        assert error.reason == "TOO_LARGE"
        assert "uncompressed" in error.message

    def test_the_total_limit_triggers_even_when_every_file_is_small(self):
        files = {f"f{index}.bin": bytes([index]) * 1000 for index in range(10)}
        data = make_skill_zip(files=files)
        limits = PackageLimits(max_file_bytes=2000, max_uncompressed_bytes=5000)
        assert reason_of(data, limits) == "TOO_LARGE"

    def test_file_count_limit_boundaries(self):
        files = {f"f{index}.txt": "x" for index in range(9)}
        data = make_skill_zip(files=files)
        assert inspect_package(data, PackageLimits(max_files=10)).file_count == 10
        error = rejection(data, PackageLimits(max_files=9))
        assert error.reason == "TOO_MANY_FILES"
        assert "9" in error.message

    def test_directories_and_ignored_entries_do_not_count_as_files(self):
        data = make_skill_zip(
            files={"a/b/c.txt": "x", "a/d.txt": "y"},
            dir_entries=True,
            macos_junk=True,
            wrapper="pkg",
        )
        assert inspect_package(data, PackageLimits(max_files=3)).file_count == 3
        assert reason_of(data, PackageLimits(max_files=2)) == "TOO_MANY_FILES"

    def test_the_default_file_count_limit_is_two_thousand(self):
        at_limit = make_skill_zip(files={f"f{index}.txt": "x" for index in range(1999)})
        assert inspect_package(at_limit).file_count == 2000
        over_limit = make_skill_zip(files={f"f{index}.txt": "x" for index in range(2000)})
        assert reason_of(over_limit) == "TOO_MANY_FILES"

    def test_the_file_count_is_checked_before_the_entrypoint(self):
        data = make_zip({f"f{index}.txt": "x" for index in range(20)})
        assert reason_of(data, PackageLimits(max_files=5)) == "TOO_MANY_FILES"

    def test_the_declared_size_rejects_an_honest_oversized_file_without_reading_it(
        self, monkeypatch
    ):
        counter = CountingHashlib()
        monkeypatch.setattr(inspector, "hashlib", counter)
        data = make_zeros_zip(8 * MIB)
        error = rejection(data, PackageLimits(max_file_bytes=1 * MIB))
        assert error.reason == "TOO_LARGE"
        assert counter.hashed == 0

    def test_limits_apply_to_wrapped_packages_identically(self):
        files = {"a.bin": bytes(range(256)) * 8}
        flat = make_skill_zip(files=files)
        wrapped = make_skill_zip(files=files, wrapper="pkg")
        limits = PackageLimits(max_file_bytes=1000)
        assert reason_of(flat, limits) == reason_of(wrapped, limits) == "TOO_LARGE"


@pytest.fixture(scope="module")
def deflate_bomb():
    return make_bomb_zip(20 * GIB, declared_size=1)


@pytest.fixture(scope="module")
def honest_bomb():
    return make_bomb_zip(3 * GIB)


class TestDecompressionBombs:
    def test_the_fixture_really_is_a_twenty_gibibyte_bomb_that_fits_the_upload_limit(
        self, deflate_bomb
    ):
        assert len(deflate_bomb) < DEFAULT_LIMITS.max_package_bytes
        assert len(deflate_bomb) > 15 * MIB
        with zipfile.ZipFile(io.BytesIO(deflate_bomb)) as archive:
            info = archive.getinfo("payload.bin")
            assert info.file_size == 1
            assert info.compress_size > 15 * MIB

    def test_a_bomb_with_a_forged_declared_size_is_aborted_at_the_limit(self, deflate_bomb):
        tracemalloc.start()
        started = time.perf_counter()
        try:
            error = rejection(deflate_bomb)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        elapsed = time.perf_counter() - started
        assert error.reason == "TOO_LARGE"
        assert "payload.bin" in error.message
        assert elapsed < 5
        assert peak < 8 * MIB

    def test_real_bytes_are_counted_and_the_abort_happens_exactly_at_the_limit(
        self, deflate_bomb, monkeypatch
    ):
        counter = CountingHashlib()
        monkeypatch.setattr(inspector, "hashlib", counter)
        limits = PackageLimits(max_file_bytes=3 * MIB, max_uncompressed_bytes=10 * MIB)
        assert reason_of(deflate_bomb, limits) == "TOO_LARGE"
        assert counter.hashed <= 3 * MIB + len(skill_md().encode("utf-8")) + 1024

    def test_a_bomb_whose_header_honestly_declares_its_size_is_rejected_without_decompressing(
        self, honest_bomb, monkeypatch
    ):
        counter = CountingHashlib()
        monkeypatch.setattr(inspector, "hashlib", counter)
        started = time.perf_counter()
        error = rejection(honest_bomb)
        assert error.reason == "TOO_LARGE"
        assert time.perf_counter() - started < 2
        assert counter.hashed < 1 * MIB

    @pytest.mark.parametrize("declared", [0, 1, 10, 999, 1 * MIB])
    def test_any_forged_declared_size_cannot_hide_oversized_content(self, declared):
        limits = PackageLimits(max_file_bytes=1 * MIB, max_uncompressed_bytes=4 * MIB)
        data = make_raw_package([zero_bomb_entry("zeros.bin", 6 * MIB, declared_size=declared)])
        assert reason_of(data, limits) == "TOO_LARGE"

    def test_patching_the_header_of_an_honest_archive_cannot_hide_oversized_content(self):
        limits = PackageLimits(max_file_bytes=1 * MIB, max_uncompressed_bytes=4 * MIB)
        honest = make_zeros_zip(6 * MIB, entry_name="zeros.bin")
        assert reason_of(honest, limits) == "TOO_LARGE"
        forged = forge_declared_size(honest, "zeros.bin", 10)
        assert reason_of(forged, limits) == "TOO_LARGE"

    def test_honest_sizes_that_add_up_beyond_the_total_limit_are_refused_without_reading(
        self, monkeypatch
    ):
        counter = CountingHashlib()
        monkeypatch.setattr(inspector, "hashlib", counter)
        entries = [stored_entry("SKILL.md", skill_md("many", "1.0.0"))]
        entries += [zero_bomb_entry(f"z{index}.bin", 1 * MIB) for index in range(8)]
        limits = PackageLimits(max_file_bytes=2 * MIB, max_uncompressed_bytes=4 * MIB)
        error = rejection(build_raw_zip(entries), limits)
        assert error.reason == "TOO_LARGE"
        assert "uncompressed" in error.message
        assert counter.hashed == 0

    def test_the_total_limit_is_enforced_on_real_bytes_when_the_declared_size_lies(
        self, monkeypatch
    ):
        counter = CountingHashlib()
        monkeypatch.setattr(inspector, "hashlib", counter)
        data = make_raw_package([zero_bomb_entry("z.bin", 6 * MIB, declared_size=1)])
        limits = PackageLimits(max_file_bytes=8 * MIB, max_uncompressed_bytes=4 * MIB)
        error = rejection(data, limits)
        assert error.reason == "TOO_LARGE"
        assert "uncompressed" in error.message
        assert counter.hashed <= 4 * MIB

    def test_forged_sizes_that_stay_within_the_limits_are_still_inconsistent(self):
        entries = [stored_entry("SKILL.md", skill_md("many", "1.0.0"))]
        entries += [
            zero_bomb_entry(f"z{index}.bin", 1 * MIB, declared_size=1) for index in range(8)
        ]
        limits = PackageLimits(max_file_bytes=2 * MIB, max_uncompressed_bytes=4 * MIB)
        assert reason_of(build_raw_zip(entries), limits) == "CORRUPT_ARCHIVE"

    def test_a_declared_size_smaller_than_the_real_one_is_rejected_even_within_limits(self):
        data = make_skill_zip(files={"a.txt": "x" * 3000})
        error = rejection(forge_declared_size(data, "a.txt", 10))
        assert error.reason == "CORRUPT_ARCHIVE"
        assert "declares" in error.message

    def test_a_declared_size_larger_than_the_real_one_is_rejected(self):
        data = make_skill_zip(files={"a.txt": "x" * 3000})
        assert reason_of(forge_declared_size(data, "a.txt", 5000)) == "CORRUPT_ARCHIVE"

    def test_a_forged_size_on_a_stored_entry_is_rejected(self):
        data = make_skill_zip(files={"a.txt": "x" * 300}, compression=zipfile.ZIP_STORED)
        assert reason_of(forge_declared_size(data, "a.txt", 10)) == "CORRUPT_ARCHIVE"
        assert reason_of(forge_declared_size(data, "a.txt", 3000)) == "CORRUPT_ARCHIVE"

    def test_a_forged_size_on_the_entrypoint_cannot_truncate_the_frontmatter_silently(self):
        data = make_skill_zip(body="b" * 400)
        assert reason_of(forge_declared_size(data, "SKILL.md", 20)) == "CORRUPT_ARCHIVE"

    def test_overlapping_entries_cannot_multiply_the_content(self):
        entries = [
            stored_entry("SKILL.md", skill_md("quine", "1.0.0")),
            zero_bomb_entry("a.bin", 3 * MIB),
            zero_bomb_entry("b.bin", 3 * MIB),
        ]
        data = build_raw_zip(entries)
        patched = set_local_header_offset(data, "b.bin", local_header_offset(data, "a.bin"))
        limits = PackageLimits(max_file_bytes=4 * MIB, max_uncompressed_bytes=5 * MIB)
        assert reason_of(patched, limits) == "CORRUPT_ARCHIVE"
        assert reason_of(patched) == "CORRUPT_ARCHIVE"

    def test_zero_length_and_tiny_files_are_not_mistaken_for_bombs(self):
        data = make_skill_zip(files={"empty.bin": b"", "one.bin": b"\x00", "z.bin": bytes(1000)})
        manifest = manifest_of(inspect_package(data))
        assert manifest["empty.bin"].size == 0
        assert manifest["one.bin"].size == 1
        assert manifest["z.bin"].size == 1000

    def test_rejection_does_not_leave_the_inspector_in_a_bad_state(self, deflate_bomb):
        assert reason_of(deflate_bomb) == "TOO_LARGE"
        assert inspect_package(make_skill_zip("after-bomb", "1.0.0")).name == "after-bomb"


class TestPerformance:
    def test_twenty_five_mebibytes_of_compressed_zeros_are_processed_in_bounded_time_and_memory(
        self,
    ):
        data = make_zeros_zip(25 * MIB)
        assert len(data) < 100 * KIB
        tracemalloc.start()
        started = time.perf_counter()
        try:
            parsed = inspect_package(data)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        elapsed = time.perf_counter() - started
        expected = hashlib.sha256()
        chunk = bytes(1 * MIB)
        for _ in range(25):
            expected.update(chunk)
        entry = manifest_of(parsed)["payload.bin"]
        assert entry.size == 25 * MIB
        assert entry.sha256 == expected.hexdigest()
        assert entry.text is False
        assert elapsed < 5
        assert peak < 8 * MIB

    def test_the_same_zeros_under_a_tight_limit_are_rejected_not_buffered(self):
        data = make_zeros_zip(25 * MIB)
        tracemalloc.start()
        try:
            error = rejection(data, PackageLimits(max_file_bytes=2 * MIB))
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert error.reason == "TOO_LARGE"
        assert peak < 4 * MIB

    def test_an_uncompressed_zip_of_twenty_five_mebibytes_of_zeros_is_refused_by_size(self):
        data = make_zip(
            {"SKILL.md": skill_md("stored", "1.0.0"), "zeros.bin": bytes(25 * MIB)},
            compression=zipfile.ZIP_STORED,
        )
        assert len(data) > DEFAULT_LIMITS.max_package_bytes
        started = time.perf_counter()
        error = rejection(data)
        assert error.reason == "TOO_LARGE"
        assert "package" in error.message
        assert time.perf_counter() - started < 1

    def test_a_package_of_two_thousand_files_is_inspected_quickly(self):
        files = {
            f"dir{index % 40}/sub{index % 7}/file{index}.txt": f"content {index}\n" * 5
            for index in range(1999)
        }
        data = make_skill_zip(files=files, dir_entries=True, macos_junk=True)
        started = time.perf_counter()
        parsed = inspect_package(data)
        assert time.perf_counter() - started < 3
        assert parsed.file_count == 2000

    def test_many_megabytes_spread_over_many_files(self):
        files = {f"f{index}.bin": bytes([index]) * (1 * MIB) for index in range(30)}
        data = make_skill_zip(files=files)
        started = time.perf_counter()
        parsed = inspect_package(data)
        assert time.perf_counter() - started < 5
        assert parsed.uncompressed_bytes > 30 * MIB

    def test_inspection_is_thread_safe(self):
        packages = [
            (
                make_skill_zip(
                    f"pkg-{index}",
                    "1.0.0",
                    files={"a.txt": f"data {index}", "b.bin": bytes([index]) * 5000},
                ),
                f"pkg-{index}",
            )
            for index in range(8)
        ]
        failures = []
        barrier = threading.Barrier(8)

        def work(data, name):
            barrier.wait()
            for _ in range(20):
                parsed = inspect_package(data)
                if parsed.name != name or parsed.file_count != 3:
                    failures.append((name, parsed.name))

        with ThreadPoolExecutor(max_workers=8) as pool:
            for future in [pool.submit(work, data, name) for data, name in packages]:
                future.result()
        assert failures == []


class TestReadPackageFile:
    @pytest.fixture
    def package(self):
        files = {
            "scripts/run.py": "print('hello')\n",
            "docs/caf" + E_ACUTE + ".md": "# cafe\n",
            "data.bin": bytes(range(256)) * 10,
            "big.txt": "line\n" * 10_000,
            "empty.txt": "",
        }
        return {
            "files": files,
            "flat": make_skill_zip("reader", "1.0.0", files=files),
            "wrapped": make_skill_zip("reader", "1.0.0", files=files, wrapper="reader-pkg"),
        }

    def test_reads_a_file_from_a_flat_package(self, package):
        assert read_package_file(package["flat"], "", "scripts/run.py") == b"print('hello')\n"

    def test_reads_a_file_from_a_wrapped_package(self, package):
        assert (
            read_package_file(package["wrapped"], "reader-pkg/", "scripts/run.py")
            == b"print('hello')\n"
        )

    def test_reads_the_entrypoint_binary_and_empty_files(self, package):
        assert read_package_file(package["flat"], "", "SKILL.md") == skill_md(
            "reader", "1.0.0"
        ).encode("utf-8")
        assert read_package_file(package["flat"], "", "data.bin") == package["files"]["data.bin"]
        assert read_package_file(package["flat"], "", "empty.txt") == b""

    def test_reads_unicode_paths(self, package):
        assert read_package_file(package["flat"], "", "docs/caf" + E_ACUTE + ".md") == b"# cafe\n"

    def test_the_content_matches_the_manifest_hash(self, package):
        parsed = inspect_package(package["wrapped"])
        for entry in parsed.manifest:
            content = read_package_file(package["wrapped"], parsed.root_prefix, entry.path)
            assert sha(content) == entry.sha256

    @pytest.mark.parametrize(
        "path",
        [
            "../secret",
            "a/../../secret",
            "/etc/passwd",
            "a\\b",
            "a\x00b",
            "",
            ".",
            "a//b",
            "./scripts/run.py",
            "C:/x",
        ],
    )
    def test_unsafe_paths_are_rejected_as_invalid_paths(self, package, path):
        with pytest.raises(ValidationError) as caught:
            read_package_file(package["flat"], "", path)
        assert caught.value.reason == "INVALID_PATH"

    def test_traversal_cannot_escape_the_wrapper(self, package):
        with pytest.raises(ValidationError):
            read_package_file(package["wrapped"], "reader-pkg/", "../reader-pkg/scripts/run.py")
        with pytest.raises(ValidationError):
            read_package_file(package["wrapped"], "reader-pkg/", "../SKILL.md")

    def test_a_missing_file_is_not_found(self, package):
        with pytest.raises(NotFoundError, match="nope.txt"):
            read_package_file(package["flat"], "", "nope.txt")

    def test_a_directory_is_not_a_file(self, package):
        data = make_skill_zip(files={"docs/a.txt": "x"}, dir_entries=True)
        with pytest.raises(NotFoundError):
            read_package_file(data, "", "docs")

    def test_a_wrong_root_prefix_finds_nothing(self, package):
        with pytest.raises(NotFoundError):
            read_package_file(package["wrapped"], "", "scripts/run.py")
        with pytest.raises(NotFoundError):
            read_package_file(package["flat"], "other/", "scripts/run.py")

    def test_ignored_entries_are_not_readable(self):
        data = make_skill_zip(files={"keep.txt": "k"}, wrapper="pkg", macos_junk=True)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            junk = [
                name for name in archive.namelist() if "._" in name or name.endswith(".DS_Store")
            ]
        assert junk
        assert read_package_file(data, "pkg/", "keep.txt") == b"k"
        with pytest.raises(NotFoundError):
            read_package_file(data, "pkg/", ".DS_Store")
        with pytest.raises(NotFoundError):
            read_package_file(data, "", "__MACOSX/pkg/._SKILL.md")

    @pytest.mark.parametrize(
        "prefix", ["pkg", "../", "a/b/", "/", "pkg//", "a\\", "x/y", ".", "./", "pkg/\x00/"]
    )
    def test_an_invalid_root_prefix_is_a_storage_error(self, package, prefix):
        with pytest.raises(StorageError, match="root prefix") as caught:
            read_package_file(package["flat"], prefix, "SKILL.md")
        assert type(caught.value) is StorageError
        assert (caught.value.code, caught.value.reason) == ("STORAGE_ERROR", None)

    def test_the_root_prefix_is_checked_before_the_archive_is_opened(self):
        with pytest.raises(StorageError):
            read_package_file(b"this is not a zip", "../", "SKILL.md")

    def test_max_bytes_is_a_hard_cap(self, package):
        content_size = len(package["files"]["big.txt"])
        assert (
            len(read_package_file(package["flat"], "", "big.txt", max_bytes=content_size))
            == content_size
        )
        with pytest.raises(InvalidPackageError) as caught:
            read_package_file(package["flat"], "", "big.txt", max_bytes=content_size - 1)
        assert caught.value.reason == "TOO_LARGE"

    def test_a_cap_of_zero_allows_only_empty_files(self, package):
        assert read_package_file(package["flat"], "", "empty.txt", max_bytes=0) == b""
        with pytest.raises(InvalidPackageError) as caught:
            read_package_file(package["flat"], "", "scripts/run.py", max_bytes=0)
        assert caught.value.reason == "TOO_LARGE"

    @pytest.mark.parametrize("cap", [-1, -2, -(1 << 40)])
    @pytest.mark.parametrize("truncate", [False, True])
    def test_negative_caps_are_a_validation_error(self, package, cap, truncate):
        with pytest.raises(ValidationError, match="max_bytes") as caught:
            read_package_file(package["flat"], "", "SKILL.md", max_bytes=cap, truncate=truncate)
        assert type(caught.value) is ValidationError
        assert (caught.value.code, caught.value.reason) == ("VALIDATION_ERROR", None)

    def test_no_bare_value_error_escapes_from_the_reader(self, package):
        hostile_calls = [
            ("", "SKILL.md", {"max_bytes": -1}),
            ("../", "SKILL.md", {}),
            ("a/b/", "SKILL.md", {}),
            ("", "../SKILL.md", {}),
            ("", "SKILL.md\x00", {}),
            ("", "nope.txt", {}),
        ]
        for prefix, path, options in hostile_calls:
            with pytest.raises(DomainError) as caught:
                read_package_file(package["flat"], prefix, path, **options)
            assert type(caught.value) is not ValueError

    def test_truncate_returns_exactly_the_requested_prefix(self, package):
        full = package["files"]["big.txt"].encode("utf-8")
        prefix = read_package_file(package["flat"], "", "big.txt", max_bytes=1234, truncate=True)
        assert prefix == full[:1234]
        assert (
            read_package_file(package["flat"], "", "big.txt", max_bytes=len(full), truncate=True)
            == full
        )
        assert (
            read_package_file(
                package["flat"], "", "big.txt", max_bytes=len(full) + 99, truncate=True
            )
            == full
        )
        assert read_package_file(package["flat"], "", "big.txt", max_bytes=0, truncate=True) == b""

    def test_truncation_across_chunk_boundaries(self):
        content = bytes(range(256)) * 1000
        data = make_skill_zip(files={"f.bin": content}, compression=zipfile.ZIP_STORED)
        for size in (1, 65535, 65536, 65537, 131072, 200_000):
            assert (
                read_package_file(data, "", "f.bin", max_bytes=size, truncate=True)
                == content[:size]
            )

    def test_a_flipped_byte_fails_the_crc_check(self):
        data = make_skill_zip(files={"a.bin": bytes(range(200))}, compression=zipfile.ZIP_STORED)
        broken = corrupt_entry_data(data, "a.bin", index=10)
        with pytest.raises(InvalidPackageError) as caught:
            read_package_file(broken, "", "a.bin")
        assert caught.value.reason == "CORRUPT_ARCHIVE"
        assert read_package_file(broken, "", "SKILL.md")

    def test_a_forged_declared_size_cannot_truncate_the_read_silently(self):
        data = make_skill_zip(files={"a.txt": "x" * 3000})
        forged = forge_declared_size(data, "a.txt", 10)
        with pytest.raises(InvalidPackageError) as caught:
            read_package_file(forged, "", "a.txt")
        assert caught.value.reason == "CORRUPT_ARCHIVE"

    def test_reading_a_bomb_without_a_cap_stops_at_the_declared_size(self):
        data = make_raw_package([zero_bomb_entry("z.bin", 3 * GIB, declared_size=2000)])
        started = time.perf_counter()
        with pytest.raises(InvalidPackageError) as caught:
            read_package_file(data, "", "z.bin")
        assert caught.value.reason == "CORRUPT_ARCHIVE"
        assert time.perf_counter() - started < 1

    def test_reading_a_bomb_with_a_cap_stops_at_the_cap(self):
        data = make_raw_package([zero_bomb_entry("z.bin", 3 * GIB, declared_size=3 * GIB - 1)])
        started = time.perf_counter()
        with pytest.raises(InvalidPackageError) as caught:
            read_package_file(data, "", "z.bin", max_bytes=1 * MIB)
        assert caught.value.reason == "TOO_LARGE"
        assert time.perf_counter() - started < 1
        prefix = read_package_file(data, "", "z.bin", max_bytes=100, truncate=True)
        assert prefix == bytes(100)

    def test_symlinks_encrypted_and_exotic_entries_are_refused(self):
        data = make_raw_package(
            [
                stored_entry("link", "target", external_attr=UNIX_SYMLINK),
                stored_entry("locked", b"\x01" * 10, flags=0x1),
                stored_entry("exotic", b"x", method=12),
            ]
        )
        for name, reason in (
            ("link", "SYMLINK_NOT_ALLOWED"),
            ("locked", "ENCRYPTED_ENTRY"),
            ("exotic", "UNSUPPORTED_COMPRESSION"),
        ):
            with pytest.raises(InvalidPackageError) as caught:
                read_package_file(data, "", name)
            assert caught.value.reason == reason

    def test_duplicate_entries_in_a_stored_blob_are_refused(self):
        data = make_zip(
            [("SKILL.md", skill_md("dup", "1.0.0")), ("a.txt", "first"), ("a.txt", "second")]
        )
        with pytest.raises(InvalidPackageError) as caught:
            read_package_file(data, "", "a.txt")
        assert caught.value.reason == "DUPLICATE_PATH"

    def test_conflicting_names_in_a_stored_blob_are_refused(self):
        extra = unicode_path_extra("safe.txt", "other.txt")
        data = build_raw_zip([stored_entry("safe.txt", "x", extra=extra)])
        with pytest.raises(InvalidPackageError) as caught:
            read_package_file(data, "", "safe.txt")
        assert caught.value.reason == "UNSAFE_PATH"

    def test_a_blob_that_is_not_a_zip(self):
        with pytest.raises(InvalidPackageError) as caught:
            read_package_file(b"not a zip", "", "SKILL.md")
        assert caught.value.reason == "NOT_A_ZIP"

    def test_the_root_prefix_for_a_zip_with_a_comment_and_extra_fields(self):
        data = build_raw_zip(
            [stored_entry("pkg/a.txt", "data", extra=b"UT\x05\x00\x03" + bytes(5))],
            comment=b"hello",
        )
        assert read_package_file(data, "pkg/", "a.txt") == b"data"

    def test_reading_leaves_the_input_untouched(self, package):
        snapshot = bytes(package["flat"])
        read_package_file(package["flat"], "", "SKILL.md")
        assert package["flat"] == snapshot


class TestFuzzing:
    def test_random_corruption_only_ever_raises_domain_errors(self):
        limits = PackageLimits(
            max_package_bytes=1 * MIB,
            max_uncompressed_bytes=4 * MIB,
            max_file_bytes=2 * MIB,
            max_files=50,
        )
        bases = [
            make_skill_zip(
                "fuzz-a",
                "1.0.0",
                tags=["a", "b"],
                files={"scripts/run.py": "print(1)\n" * 20, "data.bin": bytes(range(256)) * 4},
                dir_entries=True,
                macos_junk=True,
                wrapper="pkg",
            ),
            make_skill_zip(
                "fuzz-b", "2.0.0-rc.1", files={"a.txt": "hello", "b/c.txt": "world" * 100}
            ),
            make_grounding_zip(
                "fuzz-c", "0.1.0", files={"x.csv": "a,b\n1,2\n"}, compression=zipfile.ZIP_STORED
            ),
        ]
        rng = random.Random(20260101)
        outcomes = set()
        for _ in range(3000):
            data = bytearray(rng.choice(bases))
            for _ in range(rng.choice((1, 1, 2, 3, 8))):
                if not data:
                    data = bytearray(b"x")
                position = rng.randrange(len(data))
                roll = rng.random()
                if roll < 0.5:
                    data[position] ^= 1 << rng.randrange(8)
                elif roll < 0.75:
                    data[position] = rng.randrange(256)
                elif roll < 0.85:
                    del data[position : position + rng.randrange(1, 40)]
                elif roll < 0.95:
                    data[position:position] = bytes(
                        rng.randrange(256) for _ in range(rng.randrange(1, 20))
                    )
                else:
                    data = data[: max(1, position)]
            blob = bytes(data)
            try:
                inspect_package(blob, limits)
                outcomes.add("ok")
            except DomainError as error:
                outcomes.add(error.reason)
            try:
                read_package_file(blob, "pkg/", "scripts/run.py", max_bytes=1000)
            except DomainError:
                pass
        assert "ok" in outcomes
        assert {"CORRUPT_ARCHIVE", "NOT_A_ZIP"} <= outcomes
        assert outcomes <= set(PACKAGE_REASONS) | {"ok", None}

    def test_random_frontmatter_text_only_ever_raises_domain_errors(self):
        seed = (
            "---\nname: fuzz-text\nversion: 1.0.0\ndescription: A fuzz target\ntags: [a, b]\n"
            "category: tools\nextra:\n  nested: {k: v}\n  list: [1, 2.5, true, null]\n"
            "date: 2024-05-17\n---\n# Title\n\nBody text here.\n"
        )
        tokens = [
            "&a ",
            "*a",
            "!!python/object:os.system ",
            "!!binary ",
            "!!set ",
            ": ",
            "- ",
            "[",
            "]",
            "{",
            "}",
            '"',
            "'",
            "\n",
            "---\n",
            "...\n",
            "\x1b",
            "<<: ",
            "? ",
            "|\n  ",
            ">\n  ",
            "#",
            "%",
            "@",
            "\t",
            "\r\n",
            chr(0x85),
            chr(0x2028),
            chr(0xFEFF),
            "9" * 5000,
            "2023-13-45",
            ".inf",
            ".nan",
            "name: dup\n",
            "version: 1.0\n",
            "tags: x\n",
            "kind: grounding\n",
            "version: ",
            "name: ",
            "description: ",
            "tags: ",
            "category: ",
            "display_name: ",
            "kind: ",
            "\\ud800",
            "\\x00",
            "[" * 2000,
            "{a: " * 500,
        ]
        rng = random.Random(77)
        reasons = set()
        started = time.perf_counter()
        for _ in range(1500):
            text = seed
            for _ in range(rng.choice((1, 2, 3, 5))):
                position = rng.randrange(len(text) + 1)
                roll = rng.random()
                if roll < 0.6:
                    text = text[:position] + rng.choice(tokens) + text[position:]
                elif roll < 0.8:
                    text = text[:position] + text[position + rng.randrange(1, 30) :]
                else:
                    text = text[:position] + chr(rng.randrange(1, 0x2FF)) + text[position:]
            try:
                parsed = inspect_text(text)
            except DomainError as error:
                reasons.add(error.reason)
                continue
            reasons.add("ok")
            json.dumps(parsed.metadata, allow_nan=False)
            parsed.description.encode("utf-8")
            parsed.display_name.encode("utf-8")
            parsed.search_text.encode("utf-8")
        assert time.perf_counter() - started < 60
        assert "ok" in reasons
        assert {"BAD_FRONTMATTER", "INVALID_FIELD", "MISSING_FIELD"} <= reasons


class TestReasonCatalog:
    @staticmethod
    def cases():
        flat_with_binary = make_skill_zip(
            files={"a.bin": bytes(range(200))}, compression=zipfile.ZIP_STORED
        )
        big = make_skill_zip(files={"big.bin": bytes(5000)})
        return {
            "NOT_A_ZIP": (b"definitely not a zip file", DEFAULT_LIMITS, {}),
            "CORRUPT_ARCHIVE": (
                corrupt_entry_data(flat_with_binary, "a.bin", index=30),
                DEFAULT_LIMITS,
                {},
            ),
            "ENCRYPTED_ENTRY": (make_encrypted_zip(), DEFAULT_LIMITS, {}),
            "UNSUPPORTED_COMPRESSION": (
                set_compression_method(flat_with_binary, "a.bin", 12),
                DEFAULT_LIMITS,
                {},
            ),
            "SYMLINK_NOT_ALLOWED": (make_symlink_zip(), DEFAULT_LIMITS, {}),
            "UNSAFE_PATH": (make_traversal_zip(), DEFAULT_LIMITS, {}),
            "DUPLICATE_PATH": (
                make_zip([("SKILL.md", skill_md()), ("a", "1"), ("a", "2")]),
                DEFAULT_LIMITS,
                {},
            ),
            "TOO_MANY_FILES": (
                make_skill_zip(files={"a": "1", "b": "2"}),
                PackageLimits(max_files=2),
                {},
            ),
            "TOO_LARGE": (big, PackageLimits(max_file_bytes=1000), {}),
            "MISSING_ENTRYPOINT": (make_zip({"a.txt": "x"}), DEFAULT_LIMITS, {}),
            "AMBIGUOUS_ENTRYPOINT": (
                make_zip({"SKILL.md": skill_md(), "INDEX.md": index_md()}),
                DEFAULT_LIMITS,
                {},
            ),
            "BAD_FRONTMATTER": (make_zip({"SKILL.md": "no frontmatter"}), DEFAULT_LIMITS, {}),
            "MISSING_FIELD": (
                make_zip({"SKILL.md": frontmatter_doc("name: a\nversion: 1.0.0\n")}),
                DEFAULT_LIMITS,
                {},
            ),
            "INVALID_FIELD": (make_skill_zip("BAD NAME"), DEFAULT_LIMITS, {}),
            "KIND_MISMATCH": (make_skill_zip(kind="grounding"), DEFAULT_LIMITS, {}),
            "VERSION_MISMATCH": (
                make_skill_zip(version="1.0.0"),
                DEFAULT_LIMITS,
                {"version": "2.0.0"},
            ),
        }

    def test_every_contract_reason_has_a_case(self):
        assert set(self.cases()) == set(PACKAGE_REASONS)

    @pytest.mark.parametrize("reason", PACKAGE_REASONS)
    def test_each_reason_is_produced_by_a_real_malformed_package(self, reason):
        data, limits, kwargs = self.cases()[reason]
        error = rejection(data, limits, **kwargs)
        assert error.reason == reason
        assert error.code == "INVALID_PACKAGE"
        assert error.message
        assert reason in PACKAGE_REASONS

    def test_a_valid_control_package_is_accepted_with_the_same_builders(self):
        assert inspect_package(make_skill_zip()).name == "sample-skill"
        assert inspect_package(make_grounding_zip()).name == "sample-grounding"
