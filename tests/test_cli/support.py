from __future__ import annotations

import hashlib
import io
import json
import os
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx

from app.cli import main
from app.models.schemas import PaginatedResult, SkillSummary, UploadResult, VersionSummary
from app.services import semver
from app.services.errors import InvalidPackageError, ValidationError
from app.services.package_inspector import ParsedPackage, inspect_package
from tests.helpers.packages import skill_md

AUTO = "auto"
LOCAL_URL = "http://localhost:8000"
DOWNLOAD_PATH = re.compile(r"^/api/v1/skills/(?P<ref>[^/]+)/versions/(?P<version>[^/]+)/download$")


@dataclass(frozen=True)
class Result:
    code: int
    out: str
    err: str

    def json(self) -> Any:
        return json.loads(self.out)

    def error_json(self) -> Any:
        return json.loads(self.err)


def remote_env(token: str | None = "alx_write", url: str = LOCAL_URL) -> dict[str, str]:
    env = {"ALX_URL": url}
    if token is not None:
        env["ALX_TOKEN"] = token
    return env


def run(
    *argv: object,
    transport: httpx.BaseTransport | None = None,
    env: Mapping[str, str] | None = None,
) -> Result:
    out, err = io.StringIO(), io.StringIO()
    code = main(
        [str(item) for item in argv],
        transport=transport,
        env={} if env is None else env,
        stdout=out,
        stderr=err,
    )
    return Result(code, out.getvalue(), err.getvalue())


def parse_form(request: httpx.Request) -> dict[str, bytes]:
    content_type = request.headers["content-type"]
    boundary = re.search(r'boundary=(?:"([^"]+)"|([^;\s]+))', content_type)
    assert boundary is not None
    delimiter = b"--" + (boundary.group(1) or boundary.group(2)).encode("ascii")
    fields: dict[str, bytes] = {}
    for raw in request.content.split(delimiter)[1:-1]:
        raw = raw.removeprefix(b"\r\n")
        head, _, content = raw.partition(b"\r\n\r\n")
        name = re.search(rb'name="([^"]*)"', head)
        assert name is not None
        fields[name.group(1).decode("ascii")] = content.removesuffix(b"\r\n")
    return fields


def form_filename(request: httpx.Request) -> str | None:
    match = re.search(rb'filename="([^"]*)"', request.content)
    return match.group(1).decode() if match else None


def parse_fields(lines: list[str]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in lines:
        match = re.match(r"^ {2}(\S.*?) {2,}(.*)$", line)
        if match:
            fields[match.group(1)] = match.group(2)
    return fields


def write_tree(
    root: Path,
    files: Mapping[str, str | bytes],
    *,
    executable: tuple[str, ...] = (),
    modes: Mapping[str, int] | None = None,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
        target.chmod(0o755 if relative in executable else 0o644)
    for relative, mode in (modes or {}).items():
        (root / relative).chmod(mode)
    return root


def skill_tree(
    root: Path,
    name: str = "demo-skill",
    version: str | None = "1.0.0",
    *,
    files: Mapping[str, str | bytes] | None = None,
    executable: tuple[str, ...] = (),
    **frontmatter: Any,
) -> Path:
    entries: dict[str, str | bytes] = {
        "SKILL.md": skill_md(name, version, **frontmatter),
        "scripts/run.sh": "#!/bin/sh\necho demo\n",
        "references/notes.md": "# Notes\n\nSome reference text.\n",
    }
    entries.update(files or {})
    return write_tree(root, entries, executable=executable or ("scripts/run.sh",))


def tree_snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    snapshot: dict[str, tuple[bytes, int]] = {}
    for current, _, names in os.walk(root):
        for name in names:
            path = Path(current) / name
            relative = path.relative_to(root).as_posix()
            snapshot[relative] = (path.read_bytes(), path.stat().st_mode & 0o777)
    return snapshot


@dataclass
class StoredVersion:
    parsed: ParsedPackage
    data: bytes
    status: str
    version_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class StoredEntry:
    name: str
    kind: str
    skill_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    versions: dict[str, StoredVersion] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class Override:
    data: bytes
    sha256: str | None
    headers: dict[str, str]
    status: int = 200


class FakeRegistry:
    OWNER_ID = "00000000-0000-4000-8000-000000000001"

    def __init__(
        self,
        *,
        tokens: Mapping[str, str] | None = None,
        public_read: bool = True,
        prefix: str = "",
    ) -> None:
        self.prefix = prefix
        self.tokens = dict(tokens or {"alx_write": "write", "alx_read": "read"})
        self.public_read = public_read
        self.entries: dict[str, StoredEntry] = {}
        self.requests: list[httpx.Request] = []
        self.overrides: dict[tuple[str, str], Override] = {}
        self.failure: Exception | None = None
        self.redirect_to: str | None = None
        self.raw_response: httpx.Response | None = None
        self.transport = httpx.MockTransport(self.handle)

    def add_package(self, data: bytes, *, status: str = "published") -> ParsedPackage:
        parsed = inspect_package(data)
        entry = self.entries.setdefault(parsed.name, StoredEntry(parsed.name, parsed.kind))
        entry.versions[parsed.version] = StoredVersion(parsed, data, status)
        return parsed

    def serve(
        self,
        name: str,
        version: str,
        data: bytes,
        *,
        sha256: str | None = AUTO,
        headers: Mapping[str, str] | None = None,
        status: int = 200,
    ) -> None:
        if sha256 == AUTO:
            sha256 = hashlib.sha256(data).hexdigest()
        self.overrides[(name, version)] = Override(data, sha256, dict(headers or {}), status)

    def last_request(self) -> httpx.Request:
        return self.requests[-1]

    def requests_to(self, method: str, path: str) -> list[httpx.Request]:
        return [
            request
            for request in self.requests
            if request.method == method and request.url.path == path
        ]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        if self.raw_response is not None:
            return self.raw_response
        if self.redirect_to is not None:
            return httpx.Response(301, headers={"Location": self.redirect_to})
        path = request.url.path
        if not path.startswith(self.prefix):
            return self._error(404, "NOT_FOUND", f"No route for {request.method} {path}")
        path = path[len(self.prefix) :]
        if request.method == "POST" and path == "/api/v1/packages":
            return self._upload(request)
        if request.method == "GET" and path == "/api/v1/skills":
            return self._search(request)
        download = DOWNLOAD_PATH.match(path)
        if request.method == "GET" and download:
            return self._download(request, unquote(download["ref"]), unquote(download["version"]))
        return self._error(404, "NOT_FOUND", f"No route for {request.method} {path}")

    @staticmethod
    def _error(
        status: int,
        code: str,
        message: str,
        reason: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        error = {"code": code, "message": message}
        if reason:
            error["reason"] = reason
        return httpx.Response(status, json={"error": error}, headers=dict(headers or {}))

    def _scope(self, request: httpx.Request) -> str | None:
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return None
        return self.tokens.get(header[len("Bearer ") :])

    def _unauthorized(self) -> httpx.Response:
        return self._error(
            401,
            "UNAUTHORIZED",
            "Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    def _upload(self, request: httpx.Request) -> httpx.Response:
        scope = self._scope(request)
        if scope is None:
            return self._unauthorized()
        if scope != "write":
            return self._error(403, "FORBIDDEN", "This credential has read-only scope")
        form = parse_form(request)
        version = form["version"].decode() if "version" in form else None
        publish = form.get("publish") == b"true"
        data = form["file"]
        try:
            parsed = inspect_package(data, version=version)
        except InvalidPackageError as exc:
            return self._error(422, "INVALID_PACKAGE", exc.message, exc.reason)
        except ValidationError as exc:
            return self._error(422, "VALIDATION_ERROR", exc.message, exc.reason)
        entry = self.entries.get(parsed.name)
        if entry is not None and parsed.version in entry.versions:
            return self._error(
                409, "CONFLICT", f"Version {parsed.version} of '{parsed.name}' already exists"
            )
        if entry is not None and entry.kind != parsed.kind:
            return self._error(409, "CONFLICT", f"'{parsed.name}' is a {entry.kind}")
        entry = self.entries.setdefault(parsed.name, StoredEntry(parsed.name, parsed.kind))
        stored = StoredVersion(parsed, data, "published" if publish else "draft")
        entry.versions[parsed.version] = stored
        payload = UploadResult(
            skill=self._summary(entry), version=self._version_summary(entry, stored)
        )
        return httpx.Response(201, json=payload.model_dump(mode="json"))

    def _latest(self, entry: StoredEntry) -> str | None:
        published = [
            number for number, stored in entry.versions.items() if stored.status == "published"
        ]
        return semver.pick_latest(published)

    def _summary(self, entry: StoredEntry) -> SkillSummary:
        latest = self._latest(entry)
        source = entry.versions[latest or max(entry.versions, key=semver.sort_key)].parsed
        return SkillSummary(
            id=entry.skill_id,
            name=entry.name,
            display_name=source.display_name,
            description=source.description,
            kind=entry.kind,
            category=source.category,
            tags=source.tags,
            status="published" if latest else "draft",
            latest_version=latest,
            owner_id=self.OWNER_ID,
            created_at=entry.created_at,
            updated_at=entry.created_at,
        )

    def _version_summary(self, entry: StoredEntry, stored: StoredVersion) -> VersionSummary:
        parsed = stored.parsed
        return VersionSummary(
            id=stored.version_id,
            skill_id=entry.skill_id,
            version=parsed.version,
            status=stored.status,
            sha256=parsed.sha256,
            size_bytes=parsed.size_bytes,
            uncompressed_bytes=parsed.uncompressed_bytes,
            file_count=parsed.file_count,
            entrypoint=parsed.entrypoint,
            display_name=parsed.display_name,
            description=parsed.description,
            category=parsed.category,
            tags=parsed.tags,
            uploaded_by=self.OWNER_ID,
            created_at=stored.created_at,
        )

    def _search(self, request: httpx.Request) -> httpx.Response:
        scope = self._scope(request)
        if scope is None and not self.public_read:
            return self._unauthorized()
        params = request.url.params
        terms = params.get("q", "").lower().split()
        kind = params.get("kind")
        tags = params.get_list("tag")
        limit, offset = int(params.get("limit", "20")), int(params.get("offset", "0"))
        matches: list[SkillSummary] = []
        for name in sorted(self.entries):
            summary = self._summary(self.entries[name])
            if summary.latest_version is None or (kind and summary.kind != kind):
                continue
            haystack = f"{summary.name} {summary.display_name} {summary.description}".lower()
            haystack += " " + " ".join(summary.tags)
            if all(term in haystack for term in terms) and all(tag in summary.tags for tag in tags):
                matches.append(summary)
        page = PaginatedResult[SkillSummary](
            items=matches[offset : offset + limit], total=len(matches), limit=limit, offset=offset
        )
        return httpx.Response(200, json=page.model_dump(mode="json"))

    def _download(self, request: httpx.Request, ref: str, version: str) -> httpx.Response:
        scope = self._scope(request)
        if scope is None and not self.public_read:
            return self._unauthorized()
        override = self.overrides.get((ref, version))
        if override is not None:
            headers = dict(override.headers)
            if override.sha256 is not None:
                headers["X-Content-SHA256"] = override.sha256
            return httpx.Response(override.status, content=override.data, headers=headers)
        entry = self.entries.get(ref) or next(
            (item for item in self.entries.values() if item.skill_id == ref), None
        )
        if entry is None:
            return self._error(404, "NOT_FOUND", f"Skill not found: '{ref}'")
        number = self._latest(entry) if version == "latest" else version
        stored = entry.versions.get(number) if number else None
        if stored is None or (stored.status == "draft" and scope != "write"):
            return self._error(404, "NOT_FOUND", f"Version not found: '{version}'")
        sha256 = hashlib.sha256(stored.data).hexdigest()
        return httpx.Response(
            200,
            content=stored.data,
            headers={
                "Content-Type": "application/zip",
                "Content-Disposition": f'attachment; filename="{entry.name}-{number}.zip"',
                "X-Content-SHA256": sha256,
                "ETag": f'"{sha256}"',
            },
        )
