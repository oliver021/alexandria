from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from httpx import AsyncClient, Response
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Base
from app.models.schemas import TokenCreate
from app.services.errors import DomainError, ForbiddenError, NotFoundError, UnauthorizedError
from app.services.package_inspector import PackageLimits
from app.services.package_service import PackageService
from app.services.skill_service import SkillService
from app.services.token_service import TokenService
from app.services.version_service import VersionService
from app.storage import LocalBlobStore
from tests.helpers.factories import Actors, make_actors
from tests.helpers.packages import make_grounding_zip, make_skill_zip

API = "/api/v1"
PUBLIC_URL = "https://registry.example.test"
JWT_SECRET = "api-tests-signing-secret-0123456789"

UNSET: Any = object()

TOKEN_ACTORS = (
    "stranger",
    "stranger_read",
    "owner",
    "owner_read",
    "admin",
    "admin_read",
)

LEAK_PATTERNS = (
    re.compile(r"Traceback"),
    re.compile(r'File "'),
    re.compile(r"site-packages"),
    re.compile(r"/home/|/tmp/|/var/"),
    re.compile(r"app[./](services|api|storage|repositories)"),
    re.compile(r"sqlalchemy|sqlite|aiosqlite", re.IGNORECASE),
)

PUBLISHED_ENTRY = "pub-skill"
DRAFT_ENTRY = "draft-skill"
HIDDEN_ENTRY = "hidden-ground"
FOREIGN_ENTRY = "foreign-skill"

HOSTILE_FILES: dict[str, str | bytes] = {
    "index.html": "<!doctype html><script>fetch('/api/v1/auth/me')</script><h1>hi</h1>",
    "icon.svg": (
        '<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)">'
        "<script>alert(document.domain)</script></svg>"
    ),
    "app.js": "fetch('/api/v1/auth/me', {credentials: 'include'});\n",
    "page.htm": "<html><body onload=alert(1)></body></html>",
    "style.css": "body { background: url(javascript:alert(1)) }\n",
    "data.xml": '<?xml version="1.0"?><x xmlns="http://www.w3.org/1999/xhtml"><script/></x>',
    "binary.html": b"\x00<script>alert(1)</script>",
}

PACKAGE_FILES: dict[str, str | bytes] = {
    "docs/guide.md": "# Guide\n\nHello there\n",
    "scripts/run.py": "print('hi')\n",
    "data/table.bin": bytes(range(256)),
    "notes/ünïcode.txt": "héllo wörld\n",
    'odd/we"ird.bin': bytes([0, 1, 2, 3, 0xFF]),
    "data/dönnées.bin": bytes([0, 7, 255]),
    "empty.txt": "",
    **HOSTILE_FILES,
}

BINARY_PATHS = tuple(
    path for path, content in PACKAGE_FILES.items() if isinstance(content, bytes) and content
)


class Api:
    def __init__(self, client: AsyncClient, tokens: Mapping[str, str | None]) -> None:
        self.client = client
        self.tokens = dict(tokens)

    def headers(self, actor: str | None = "anonymous", **extra: str) -> dict[str, str]:
        token = self.tokens.get(actor) if actor else None
        headers = dict(extra)
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    @staticmethod
    def url(path: str) -> str:
        return path if path.startswith(("/api/", "/docs", "/openapi")) else f"{API}{path}"

    async def request(
        self,
        method: str,
        path: str,
        actor: str | None = "anonymous",
        *,
        headers: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> Response:
        merged = {**self.headers(actor), **(headers or {})}
        return await self.client.request(method, self.url(path), headers=merged, **kwargs)

    async def get(self, path: str, actor: str | None = "anonymous", **kwargs: Any) -> Response:
        return await self.request("GET", path, actor, **kwargs)

    async def post(self, path: str, actor: str | None = "anonymous", **kwargs: Any) -> Response:
        return await self.request("POST", path, actor, **kwargs)

    async def delete(self, path: str, actor: str | None = "anonymous", **kwargs: Any) -> Response:
        return await self.request("DELETE", path, actor, **kwargs)

    async def upload(
        self,
        data: bytes,
        actor: str | None = "owner",
        *,
        version: str | None = None,
        publish: bool | str | None = None,
        filename: str = "package.zip",
        **kwargs: Any,
    ) -> Response:
        form: dict[str, str] = {}
        if version is not None:
            form["version"] = version
        if publish is not None:
            form["publish"] = publish if isinstance(publish, str) else str(publish).lower()
        return await self.post(
            "/packages",
            actor,
            files={"file": (filename, data, "application/zip")},
            data=form,
            **kwargs,
        )


@dataclass
class Catalog:
    packages: dict[tuple[str, str], bytes] = field(default_factory=dict)
    owners: dict[str, str] = field(default_factory=dict)

    def zip_of(self, name: str, version: str) -> bytes:
        return self.packages[(name, version)]


async def create_tokens(db: AsyncSession, actors: Actors) -> dict[str, str | None]:
    service = TokenService(db)
    tokens: dict[str, str | None] = {"anonymous": None}
    for name in TOKEN_ACTORS:
        scope = "read" if name.endswith("_read") else "write"
        principal = actors.get(name.removesuffix("_read"))
        created = await service.create(principal, TokenCreate(name=name, scope=scope))
        tokens[name] = created.token
    return tokens


async def build_catalog(
    db: AsyncSession, store: LocalBlobStore, actors: Actors, *, mini: bool = False
) -> Catalog:
    service = PackageService(db, store, limits=PackageLimits(), auto_publish=False)
    catalog = Catalog()
    files = {"docs/guide.md": PACKAGE_FILES["docs/guide.md"]} if mini else PACKAGE_FILES

    async def add(
        principal: Any,
        name: str,
        version: str,
        *,
        publish: bool,
        kind: str = "skill",
        **options: Any,
    ) -> None:
        options.setdefault("files", files)
        options.setdefault("body", f"# {name} {version}\n\nBody of {name}.\n")
        maker = make_grounding_zip if kind == "grounding" else make_skill_zip
        data = maker(name, version, **options)
        await service.upload(principal, data, publish=publish)
        catalog.packages[(name, version)] = data

    owner, stranger = actors.owner, actors.stranger
    common = {"tags": ["alpha", "beta"], "category": "ops"}
    if mini:
        await add(owner, PUBLISHED_ENTRY, "1.0.0", publish=True, **common)
        await add(owner, PUBLISHED_ENTRY, "1.1.0", publish=True, **common)
    else:
        await add(owner, PUBLISHED_ENTRY, "0.9.0", publish=True, **common)
        await add(owner, PUBLISHED_ENTRY, "1.0.0", publish=True, **common)
        await add(owner, PUBLISHED_ENTRY, "1.1.0", publish=True, **common)
        await add(owner, PUBLISHED_ENTRY, "2.0.0-beta.1", publish=True, **common)
        await add(owner, PUBLISHED_ENTRY, "2.0.0", publish=False, **common)
        await VersionService(db).deprecate(owner, PUBLISHED_ENTRY, "0.9.0")
    await add(owner, DRAFT_ENTRY, "0.1.0", publish=False, tags=["wip"], category="lab")
    await add(
        owner,
        HIDDEN_ENTRY,
        "1.0.0",
        publish=True,
        kind="grounding",
        tags=["archive"],
        category="ops",
    )
    await SkillService(db).hide(owner, HIDDEN_ENTRY)
    await add(
        stranger, FOREIGN_ENTRY, "1.0.0", publish=True, tags=["beta", "gamma"], category="dev"
    )
    catalog.owners = {
        PUBLISHED_ENTRY: "owner",
        DRAFT_ENTRY: "owner",
        HIDDEN_ENTRY: "owner",
        FOREIGN_ENTRY: "stranger",
    }
    return catalog


BASE_TABLES = frozenset({"users", "api_tokens"})
CATALOG_TABLES = frozenset({"skills", "skill_tags", "skill_versions"})

Rows = dict[str, list[dict[str, Any]]]


@dataclass(frozen=True)
class BaseTemplate:
    actors: Actors
    tokens: dict[str, str | None]
    rows: Rows


@dataclass(frozen=True)
class CatalogTemplate:
    catalog: Catalog
    rows: Rows
    blobs: dict[str, bytes]


_BASE_TEMPLATE: BaseTemplate | None = None
_CATALOG_TEMPLATES: dict[bool, CatalogTemplate] = {}


async def capture_rows(db: AsyncSession, names: frozenset[str]) -> Rows:
    rows: Rows = {}
    for table in Base.metadata.sorted_tables:
        if table.name in names:
            result = await db.execute(select(table))
            rows[table.name] = [dict(row) for row in result.mappings()]
    return rows


async def restore_rows(db: AsyncSession, rows: Rows) -> None:
    for table in Base.metadata.sorted_tables:
        data = rows.get(table.name)
        if data:
            await db.execute(insert(table), data)
    await db.commit()


async def provide_base(db: AsyncSession) -> BaseTemplate:
    global _BASE_TEMPLATE
    if _BASE_TEMPLATE is None:
        actors = await make_actors(db)
        tokens = await create_tokens(db, actors)
        _BASE_TEMPLATE = BaseTemplate(actors, tokens, await capture_rows(db, BASE_TABLES))
    else:
        await restore_rows(db, _BASE_TEMPLATE.rows)
    return _BASE_TEMPLATE


async def provide_catalog(
    db: AsyncSession, store: LocalBlobStore, actors: Actors, *, mini: bool = False
) -> Catalog:
    template = _CATALOG_TEMPLATES.get(mini)
    if template is None:
        catalog = await build_catalog(db, store, actors, mini=mini)
        blobs = {path.stem: path.read_bytes() for path in store.root.rglob("*.zip")}
        _CATALOG_TEMPLATES[mini] = CatalogTemplate(
            catalog, await capture_rows(db, CATALOG_TABLES), blobs
        )
        return catalog
    await restore_rows(db, template.rows)
    for sha256, data in template.blobs.items():
        await store.put(sha256, data)
    return Catalog(dict(template.catalog.packages), dict(template.catalog.owners))


def assert_error(
    response: Response,
    status_code: int,
    code: str,
    *,
    reason: Any = UNSET,
) -> dict[str, Any]:
    assert response.status_code == status_code, response.text
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert set(body) == {"error"}, body
    error = body["error"]
    assert error["code"] == code, body
    assert isinstance(error["message"], str) and error["message"], body
    assert set(error) <= {"code", "message", "reason"}, body
    if reason is None:
        assert "reason" not in error, body
    elif reason is not UNSET:
        assert error.get("reason") == reason, body
    if status_code == 401:
        assert response.headers.get("www-authenticate") == "Bearer"
    else:
        assert "www-authenticate" not in response.headers
    assert_no_leak(response)
    return error


def assert_no_leak(response: Response, *extra: str) -> None:
    text = response.text
    for pattern in LEAK_PATTERNS:
        assert pattern.search(text) is None, (pattern.pattern, text)
    for fragment in extra:
        assert fragment not in text, (fragment, text)


OUTCOME_STATUS: dict[type[DomainError], int] = {
    UnauthorizedError: 401,
    ForbiddenError: 403,
    NotFoundError: 404,
}


@dataclass
class Streamed:
    status: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    received: int = 0
    chunks: int = 0
    digest: Any = field(default_factory=hashlib.sha256)
    first_chunk: bytes = b""


async def stream_through_the_app(
    path: str,
    *,
    disconnect_after: int | None = None,
    method: str = "GET",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Streamed:
    from app.main import app

    outcome = Streamed()
    disconnected = asyncio.Event()
    requested = False

    async def receive():
        nonlocal requested
        if not requested:
            requested = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            outcome.status = message["status"]
            outcome.headers = {
                key.decode().lower(): value.decode() for key, value in message["headers"]
            }
        elif message["type"] == "http.response.body":
            body = message.get("body", b"")
            if body and not outcome.chunks:
                outcome.first_chunk = body
            outcome.received += len(body)
            outcome.digest.update(body)
            outcome.chunks += 1 if body else 0
            if disconnect_after is not None and outcome.chunks >= disconnect_after:
                disconnected.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"test"), *(headers or [])],
        "client": ("127.0.0.1", 4321),
        "server": ("test", 80),
    }
    try:
        await asyncio.wait_for(app(scope, receive, send), timeout=60)
    finally:
        disconnected.set()
    return outcome
