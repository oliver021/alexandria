from __future__ import annotations

import asyncio
import json
import logging
import re
import zipfile

import pytest
from sqlalchemy import update

from app.mcp import tools
from app.mcp.context import use_principal
from app.models import SkillVersion
from app.services.errors import (
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
from app.services.skill_service import SkillService
from app.storage import BlobStoreError, LocalBlobStore
from tests.helpers.packages import make_skill_zip
from tests.test_mcp.support import API, PDF, payload_of, text_of, zip_to_base64

STORAGE_TEXT = "STORAGE_ERROR: internal storage error"
INTERNAL_TEXT = "INTERNAL_ERROR: internal error"
ERROR_FORMAT = re.compile(r"[A-Z_]+: .+")


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (NotFoundError("Skill not found: 'x'"), "NOT_FOUND: Skill not found: 'x'"),
        (ConflictError("Version 1.0.0 already exists"), "CONFLICT: Version 1.0.0 already exists"),
        (ValidationError("limit is wrong"), "VALIDATION_ERROR: limit is wrong"),
        (
            ValidationError("Invalid path", reason="INVALID_PATH"),
            "VALIDATION_ERROR: Invalid path [INVALID_PATH]",
        ),
        (
            InvalidPackageError("UNSAFE_PATH"),
            "INVALID_PACKAGE: Invalid package: UNSAFE_PATH [UNSAFE_PATH]",
        ),
        (
            InvalidPackageError("TOO_LARGE", "The package expands to too many bytes"),
            "INVALID_PACKAGE: The package expands to too many bytes [TOO_LARGE]",
        ),
        (ForbiddenError("Only the owner can do it"), "FORBIDDEN: Only the owner can do it"),
        (UnauthorizedError(), "UNAUTHORIZED: Authentication required"),
        (PayloadTooLargeError("Too big"), "PAYLOAD_TOO_LARGE: Too big"),
        (DomainError("Something else"), "DOMAIN_ERROR: Something else"),
        (NotFoundError(), "NOT_FOUND: Resource not found"),
    ],
)
def test_domain_errors_are_described_as_code_message_and_an_optional_reason(error, expected):
    assert tools.describe(error) == expected


def test_a_reason_is_only_appended_when_the_error_carries_one():
    assert "[" not in tools.describe(NotFoundError("Skill not found: 'x'"))
    assert tools.describe(ValidationError("bad", reason="R1")).endswith(" [R1]")


def test_failures_are_error_results_with_a_single_text_block_and_no_structured_content():
    result = tools.failure("NOT_FOUND: nothing")

    assert result.is_error is True
    assert result.structured_content is None
    assert [block.type for block in result.content] == ["text"]


async def raising(error: BaseException, monkeypatch):
    async def fail(self, *args, **kwargs):
        raise error

    monkeypatch.setattr(SkillService, "get", fail)
    return await tools.get_skill(name="anything")


class TestDomainErrorsFromServices:
    @pytest.mark.parametrize(
        "error",
        [
            NotFoundError("gone"),
            ConflictError("clash"),
            ValidationError("invalid"),
            InvalidPackageError("BAD_FRONTMATTER"),
            ForbiddenError("no"),
            UnauthorizedError("who"),
            PayloadTooLargeError("big"),
        ],
    )
    async def test_every_domain_error_becomes_a_tool_error_with_its_code(self, error, monkeypatch):
        result = await raising(error, monkeypatch)

        assert result.is_error is True
        assert text_of(result) == tools.describe(error)
        assert text_of(result).startswith(f"{error.code}: ")

    async def test_a_reason_travels_in_brackets_after_the_message(self, monkeypatch):
        result = await raising(
            InvalidPackageError("UNSAFE_PATH", "Unsafe path 'a/../b'"), monkeypatch
        )

        assert text_of(result) == "INVALID_PACKAGE: Unsafe path 'a/../b' [UNSAFE_PATH]"


class TestUnexpectedFailures:
    async def test_an_unexpected_exception_never_leaks_its_text_or_a_traceback(
        self, monkeypatch, caplog
    ):
        secret = "password=hunter2 at /srv/alexandria/data/alexandria.db"

        with caplog.at_level(logging.ERROR, logger="app.mcp.tools"):
            result = await raising(RuntimeError(secret), monkeypatch)

        assert result.is_error is True
        assert text_of(result) == INTERNAL_TEXT
        assert "hunter2" not in text_of(result)
        records = [record for record in caplog.records if record.name == "app.mcp.tools"]
        assert records and records[0].levelno == logging.ERROR
        assert records[0].exc_info is not None
        assert "get_skill" in records[0].getMessage()
        assert secret not in records[0].getMessage()

    @pytest.mark.parametrize(
        "error", [ValueError("x"), KeyError("k"), OSError("disk"), TypeError("t")]
    )
    async def test_builtin_exceptions_are_internal_errors(self, error, monkeypatch):
        assert text_of(await raising(error, monkeypatch)) == INTERNAL_TEXT

    @pytest.mark.parametrize("error", [asyncio.CancelledError(), KeyboardInterrupt()])
    async def test_cancellation_and_interrupts_are_not_swallowed(self, error, monkeypatch):
        with pytest.raises(type(error)):
            await raising(error, monkeypatch)

    async def test_an_unexpected_failure_in_a_write_tool_is_reported_the_same_way(
        self, actors, monkeypatch
    ):
        async def explode(*args, **kwargs):
            raise RuntimeError("database exploded")

        monkeypatch.setattr(tools.PackageService, "upload", explode)

        with use_principal(actors.owner):
            result = await tools.upload_package(content_base64=zip_to_base64(make_skill_zip()))

        assert text_of(result) == INTERNAL_TEXT


@pytest.mark.usefixtures("registry")
class TestStorageErrors:
    async def corrupt_manifest(self, session_factory, column: str, value: str) -> None:
        async with session_factory() as db:
            await db.execute(update(SkillVersion).values({column: value}))
            await db.commit()

    async def current_blob(self, blob_store: LocalBlobStore, version: str = "1.1.0"):
        info = payload_of(await tools.get_download_info(name=PDF, version=version))
        return blob_store.root / info["sha256"][:2] / f"{info['sha256']}.zip"

    async def test_a_missing_blob_is_a_generic_storage_error(self, blob_store, caplog):
        path = await self.current_blob(blob_store)
        path.unlink()

        with caplog.at_level(logging.ERROR):
            entry = await tools.get_entrypoint(name=PDF)
            file = await tools.read_file(name=PDF, path="docs/guide.md")

        assert text_of(entry) == STORAGE_TEXT
        assert text_of(file) == STORAGE_TEXT
        assert entry.is_error and file.is_error
        for text in (text_of(entry), text_of(file)):
            assert path.stem not in text
            assert str(blob_store.root) not in text
        assert any(record.levelno >= logging.ERROR for record in caplog.records)

    async def test_a_corrupted_blob_is_a_generic_storage_error(self, blob_store):
        path = await self.current_blob(blob_store)
        path.write_bytes(path.read_bytes()[::-1])

        result = await tools.read_file(name=PDF, path="scripts/fill.py")

        assert text_of(result) == STORAGE_TEXT

    async def test_a_tampered_file_is_a_generic_storage_error(self, actors, blob_store):
        data = make_skill_zip(
            "tampered",
            "1.0.0",
            files={"scripts/run.py": "print('run')\n"},
            compression=zipfile.ZIP_STORED,
        )
        with use_principal(actors.owner):
            await tools.upload_package(content_base64=zip_to_base64(data), publish=True)
            info = payload_of(await tools.get_download_info(name="tampered"))
        path = blob_store.root / info["sha256"][:2] / f"{info['sha256']}.zip"
        stored = bytearray(path.read_bytes())
        stored[stored.index(b"print")] ^= 0x20
        path.write_bytes(bytes(stored))

        result = await tools.read_file(name="tampered", path="scripts/run.py")

        assert text_of(result) == STORAGE_TEXT

    @pytest.mark.parametrize("column", ["manifest", "tags"])
    async def test_unreadable_json_columns_are_a_generic_storage_error(
        self, session_factory, column
    ):
        await self.corrupt_manifest(session_factory, column, "{not json")

        results = [
            await tools.get_skill(name=PDF),
            await tools.list_files(name=PDF),
            await tools.get_entrypoint(name=PDF),
        ]

        failures = [text_of(result) for result in results if result.is_error]
        assert failures
        assert set(failures) == {STORAGE_TEXT}

    async def test_a_blob_store_failure_during_upload_is_a_generic_storage_error(
        self, actors, blob_store, session_factory, monkeypatch
    ):
        async def refuse(self, key, data):
            raise BlobStoreError(f"disk full writing {self.root}/{key}")

        monkeypatch.setattr(LocalBlobStore, "put", refuse)
        with use_principal(actors.owner):
            result = await tools.upload_package(
                content_base64=zip_to_base64(make_skill_zip("no-space", "1.0.0"))
            )

        assert text_of(result) == STORAGE_TEXT
        assert str(blob_store.root) not in text_of(result)

    async def test_other_entries_keep_working_when_one_blob_is_lost(self, blob_store):
        (await self.current_blob(blob_store)).unlink()

        healthy = await tools.get_entrypoint(name=API)

        assert healthy.is_error is False


@pytest.mark.usefixtures("registry")
class TestNoErrorEverLooksLikeATrace:
    @pytest.mark.parametrize(
        ("tool", "arguments"),
        [
            ("get_skill", {"name": "../../x"}),
            ("get_skill", {"name": "x" * 70_000}),
            ("get_entrypoint", {"name": PDF, "version": "bogus"}),
            ("get_entrypoint", {"name": "nope"}),
            ("list_files", {"name": "nope"}),
            ("read_file", {"name": PDF, "path": "../../etc/shadow"}),
            ("read_file", {"name": PDF, "path": "missing"}),
            ("read_file", {"name": "nope", "path": "SKILL.md"}),
            ("get_download_info", {"name": "nope"}),
            ("get_download_info", {"name": PDF, "version": "9.9.9"}),
            ("search_skills", {"limit": 0}),
            ("search_skills", {"offset": 2**31}),
            ("search_skills", {"kind": "nonsense"}),
            ("search_skills", {"query": "q" * 513}),
            ("search_skills", {"category": "c" * 257}),
            ("list_tags", {"limit": 0}),
            ("list_categories", {"kind": "nonsense"}),
            ("publish_version", {"name": PDF, "version": "2.0.0"}),
            ("deprecate_version", {"name": PDF, "version": "1.1.0"}),
            ("upload_package", {"content_base64": "%%%"}),
        ],
    )
    async def test_error_text_is_a_short_code_and_message(self, tool, arguments):
        result = await getattr(tools, tool)(**arguments)

        if not result.is_error:
            json.dumps(result.structured_content)
            return
        text = text_of(result)
        assert ERROR_FORMAT.fullmatch(text), text
        assert "Traceback" not in text
        assert 'File "' not in text
        assert len(text) < 600
        assert result.structured_content is None


@pytest.mark.usefixtures("registry")
class TestCallLogging:
    @staticmethod
    def calls(caplog) -> list[dict]:
        return [
            record.extra_data
            for record in caplog.records
            if record.name == "app.mcp.tools" and hasattr(record, "extra_data")
        ]

    async def test_every_call_is_logged_with_its_tool_outcome_and_caller(self, actors, caplog):
        with caplog.at_level(logging.INFO, logger="app.mcp.tools"):
            await tools.get_skill(name=PDF)
            with use_principal(actors.owner):
                await tools.get_skill(name="nothing-here")
                await tools.search_skills(limit=0)

        first, second, third = self.calls(caplog)
        assert (first["tool"], first["outcome"], first["user_id"]) == ("get_skill", "ok", None)
        assert (second["tool"], second["outcome"]) == ("get_skill", "NOT_FOUND")
        assert second["user_id"] == actors.owner.user_id
        assert (third["tool"], third["outcome"]) == ("search_skills", "VALIDATION_ERROR")
        assert all(isinstance(entry["elapsed_ms"], float) for entry in (first, second, third))

    async def test_failures_are_logged_under_their_own_outcome(self, monkeypatch, caplog):
        with caplog.at_level(logging.INFO, logger="app.mcp.tools"):
            await raising(RuntimeError("secret"), monkeypatch)
            await raising(StorageError("hidden detail"), monkeypatch)

        assert [entry["outcome"] for entry in self.calls(caplog)] == [
            "INTERNAL_ERROR",
            "STORAGE_ERROR",
        ]

    async def test_arguments_and_payloads_are_never_logged(self, actors, caplog):
        payload = zip_to_base64(
            make_skill_zip("log-check", "1.0.0", files={"s.txt": "needle-text"})
        )

        with caplog.at_level(logging.DEBUG, logger="app.mcp"), use_principal(actors.owner):
            await tools.upload_package(content_base64=payload, version=None)
            await tools.read_file(name=PDF, path="docs/guide.md")
            await tools.get_skill(name="very-specific-name-xyz")

        logged = " ".join(f"{record.getMessage()} {record.__dict__}" for record in caplog.records)
        assert payload[:60] not in logged
        assert "docs/guide.md" not in logged
        assert "very-specific-name-xyz" not in logged
        assert "needle-text" not in logged
