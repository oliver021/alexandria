from __future__ import annotations

import asyncio
import base64
import json
import re
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from httpx import AsyncClient, Response
from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.local import LocalAuthProvider
from app.models import Base
from app.models.schemas import TokenCreate
from app.repositories import user_repo
from app.services.package_inspector import PackageLimits
from app.services.package_service import PackageService
from app.services.principal import Principal
from app.services.skill_service import SkillService
from app.services.token_service import TokenService
from app.services.version_service import VersionService
from app.storage import LocalBlobStore
from tests.helpers.factories import Actors, make_actors
from tests.helpers.packages import make_grounding_zip, make_skill_zip

API = "/api/v1"
MCP_ENDPOINT = "/mcp"
ACCEPT = "application/json, text/event-stream"
CALL_TIMEOUT_SECONDS = 30
JWT_SECRET = "integration-signing-secret-0123456789"
PUBLIC_URL = "https://registry.example.test"

PUBLISHED = "pub-skill"
API_REFERENCE = "api-reference"
DRAFT = "draft-skill"
HIDDEN = "hidden-ground"
FOREIGN = "foreign-skill"
MISSING = "no-such-entry"

GUIDE_PATH = "docs/guide.md"
GUIDE_TEXT = "# Guide\n\nFill every field before flattening.\n"
LONG_PATH = "docs/long.txt"
LONG_TEXT = "0123456789abcdef" * 200
UNICODE_PATH = "docs/ñandú.md"
UNICODE_TEXT = "contenido con ñ y acentos: ÁÉÍÓÚ\n"
BINARY_PATH = "data/table.bin"
BINARY_BYTES = bytes(range(256))
EMPTY_PATH = "notes/empty.txt"

PUBLISHED_FILES: dict[str, str | bytes] = {
    GUIDE_PATH: GUIDE_TEXT,
    LONG_PATH: LONG_TEXT,
    UNICODE_PATH: UNICODE_TEXT,
    "scripts/run.py": "print('run')\n",
    BINARY_PATH: BINARY_BYTES,
    EMPTY_PATH: "",
}
API_FILES: dict[str, str | bytes] = {
    "tables/codes.csv": "code,meaning\n200,ok\n404,missing\n",
    "schemas/api.json": '{"openapi": "3.1.0"}\n',
}

STATUS_OF_CODE = {
    "UNAUTHORIZED": 401,
    "FORBIDDEN": 403,
    "NOT_FOUND": 404,
    "CONFLICT": 409,
    "PAYLOAD_TOO_LARGE": 413,
    "VALIDATION_ERROR": 422,
    "INVALID_PACKAGE": 422,
}

TOKEN_ACTORS = ("stranger", "stranger_read", "owner", "owner_read", "admin", "admin_read")
ERROR_TEXT = re.compile(
    r"^(?P<code>[A-Z_]+): (?P<message>.*?)(?: \[(?P<reason>[A-Z_]+)\])?$", re.DOTALL
)

Rows = dict[str, list[dict[str, Any]]]
McpConnector = Callable[..., AbstractAsyncContextManager[Any]]


@dataclass(frozen=True)
class Template:
    actors: Actors
    tokens: dict[str, str]
    rows: Rows
    blobs: dict[str, bytes]
    zips: dict[tuple[str, str], bytes]
    skill_ids: dict[str, str]


@dataclass
class World:
    template: Template = field(repr=False)
    factory: async_sessionmaker[AsyncSession] = field(repr=False)
    store: LocalBlobStore = field(repr=False)
    credentials: dict[str, str | None] = field(repr=False)

    @property
    def actors(self) -> Actors:
        return self.template.actors

    def principal(self, actor: str) -> Principal | None:
        return self.template.actors.get(actor)

    def zip_of(self, name: str, version: str) -> bytes:
        return self.template.zips[(name, version)]

    def skill_id(self, name: str) -> str:
        return self.template.skill_ids[name]

    async def reset(self) -> None:
        await restore_rows(self.factory, self.template.rows)


async def capture_rows(factory: async_sessionmaker[AsyncSession]) -> Rows:
    rows: Rows = {}
    async with factory() as db:
        for table in Base.metadata.sorted_tables:
            result = await db.execute(select(table))
            rows[table.name] = [dict(row) for row in result.mappings()]
    return rows


async def restore_rows(factory: async_sessionmaker[AsyncSession], rows: Rows) -> None:
    async with factory() as db:
        for table in reversed(Base.metadata.sorted_tables):
            await db.execute(delete(table))
        for table in Base.metadata.sorted_tables:
            data = rows.get(table.name)
            if data:
                await db.execute(insert(table), data)
        await db.commit()


def read_blobs(store: LocalBlobStore) -> dict[str, bytes]:
    return {path.stem: path.read_bytes() for path in store.root.rglob("*.zip")}


def restore_blobs(store: LocalBlobStore, blobs: Mapping[str, bytes]) -> None:
    for sha256, data in blobs.items():
        target = store.root / sha256[:2] / f"{sha256}.zip"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


async def issue_tokens(factory: async_sessionmaker[AsyncSession], actors: Actors) -> dict[str, str]:
    tokens: dict[str, str] = {}
    async with factory() as db:
        service = TokenService(db)
        for name in TOKEN_ACTORS:
            scope = "read" if name.endswith("_read") else "write"
            principal = actors.get(name.removesuffix("_read"))
            created = await service.create(principal, TokenCreate(name=name, scope=scope))
            tokens[name] = created.token
    return tokens


async def issue_jwt(factory: async_sessionmaker[AsyncSession], principal: Principal) -> str:
    async with factory() as db:
        user = await user_repo.get_user(db, principal.user_id)
        assert user is not None
        return await LocalAuthProvider(secret=JWT_SECRET).create_token(user)


async def seed_catalog(
    factory: async_sessionmaker[AsyncSession], store: LocalBlobStore, actors: Actors
) -> tuple[dict[tuple[str, str], bytes], dict[str, str]]:
    zips: dict[tuple[str, str], bytes] = {}
    owner, stranger = actors.owner, actors.stranger
    async with factory() as db:
        packages = PackageService(db, store, limits=PackageLimits(), auto_publish=False)
        skills = SkillService(db)

        async def add(
            principal: Principal,
            name: str,
            version: str,
            *,
            publish: bool,
            kind: str = "skill",
            **options: Any,
        ) -> None:
            maker = make_grounding_zip if kind == "grounding" else make_skill_zip
            options.setdefault("body", f"# {name} {version}\n\nBody of {name}.\n")
            data = maker(name, version, **options)
            await packages.upload(principal, data, publish=publish)
            zips[(name, version)] = data

        common: dict[str, Any] = {
            "tags": ["alpha", "beta"],
            "category": "ops",
            "description": "Fill and flatten PDF forms",
            "files": PUBLISHED_FILES,
        }
        for number in ("0.9.0", "1.0.0", "1.1.0", "2.0.0-beta.1"):
            await add(owner, PUBLISHED, number, publish=True, **common)
        await add(owner, PUBLISHED, "2.0.0", publish=False, **common)
        await VersionService(db).deprecate(owner, PUBLISHED, "0.9.0")
        await add(
            owner,
            API_REFERENCE,
            "1.0.0",
            publish=True,
            kind="grounding",
            tags=["api", "reference"],
            category="reference",
            description="Reference for the HTTP API error codes",
            files=API_FILES,
        )
        await add(owner, DRAFT, "0.1.0", publish=False, tags=["wip"], category="lab")
        await add(
            owner,
            HIDDEN,
            "1.0.0",
            publish=True,
            kind="grounding",
            tags=["archive"],
            category="ops",
        )
        await skills.hide(owner, HIDDEN)
        await add(
            stranger,
            FOREIGN,
            "1.0.0",
            publish=True,
            tags=["beta", "gamma"],
            category="dev",
            description="Read text out of scanned pages",
        )
        ids = {
            name: (await skills.get(owner, name)).id
            for name in (PUBLISHED, API_REFERENCE, DRAFT, HIDDEN)
        }
        ids[FOREIGN] = (await skills.get(stranger, FOREIGN)).id
    return zips, ids


async def build_template(
    factory: async_sessionmaker[AsyncSession], store: LocalBlobStore
) -> Template:
    async with factory() as db:
        actors = await make_actors(db)
    tokens = await issue_tokens(factory, actors)
    zips, ids = await seed_catalog(factory, store, actors)
    return Template(
        actors=actors,
        tokens=tokens,
        rows=await capture_rows(factory),
        blobs=read_blobs(store),
        zips=zips,
        skill_ids=ids,
    )


_TEMPLATE: Template | None = None


async def provide_world(factory: async_sessionmaker[AsyncSession], store: LocalBlobStore) -> World:
    global _TEMPLATE
    if _TEMPLATE is None:
        _TEMPLATE = await build_template(factory, store)
    else:
        await restore_rows(factory, _TEMPLATE.rows)
        restore_blobs(store, _TEMPLATE.blobs)
    credentials: dict[str, str | None] = {"anonymous": None, **_TEMPLATE.tokens}
    credentials["owner_jwt"] = await issue_jwt(factory, _TEMPLATE.actors.owner)
    credentials["invalid_token"] = "alx_" + "z" * 43
    credentials["invalid_jwt"] = "not-a-jwt"
    return World(_TEMPLATE, factory, store, credentials)


def authorization(credentials: Mapping[str, str | None], actor: str | None) -> dict[str, str]:
    value = credentials.get(actor) if actor else None
    return {"Authorization": f"Bearer {value}"} if value else {}


class Rest:
    def __init__(self, client: AsyncClient, credentials: Mapping[str, str | None]) -> None:
        self.client = client
        self.credentials = credentials

    async def request(
        self,
        method: str,
        path: str,
        actor: str | None = "anonymous",
        *,
        headers: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> Response:
        merged = {**authorization(self.credentials, actor), **(headers or {})}
        return await self.client.request(method, f"{API}{path}", headers=merged, **kwargs)

    async def get(self, path: str, actor: str | None = "anonymous", **kwargs: Any) -> Response:
        return await self.request("GET", path, actor, **kwargs)

    async def post(self, path: str, actor: str | None = "anonymous", **kwargs: Any) -> Response:
        return await self.request("POST", path, actor, **kwargs)

    async def upload(
        self,
        data: bytes,
        actor: str | None = "owner",
        *,
        version: str | None = None,
        publish: bool | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        form: dict[str, str] = {}
        if version is not None:
            form["version"] = version
        if publish is not None:
            form["publish"] = str(publish).lower()
        return await self.post(
            "/packages",
            actor,
            headers=headers,
            files={"file": ("package.zip", data, "application/zip")},
            data=form,
        )


@dataclass(frozen=True)
class ToolOutcome:
    status: int
    is_error: bool
    text: str
    code: str | None
    message: str | None
    reason: str | None
    payload: Any
    structured: Any

    @property
    def ok(self) -> bool:
        return not self.is_error


def parse_tool_outcome(response: Response) -> ToolOutcome:
    body = response.json()
    if response.status_code != 200:
        error = body["error"]
        return ToolOutcome(
            status=response.status_code,
            is_error=True,
            text=json.dumps(body),
            code=error["code"],
            message=error["message"],
            reason=error.get("reason"),
            payload=None,
            structured=None,
        )
    result = body["result"]
    text = result["content"][0]["text"]
    if not result.get("isError"):
        return ToolOutcome(
            status=200,
            is_error=False,
            text=text,
            code=None,
            message=None,
            reason=None,
            payload=json.loads(text),
            structured=result.get("structuredContent"),
        )
    match = ERROR_TEXT.match(text)
    return ToolOutcome(
        status=200,
        is_error=True,
        text=text,
        code=match["code"] if match else None,
        message=match["message"] if match else text,
        reason=match["reason"] if match else None,
        payload=None,
        structured=None,
    )


class Mcp:
    def __init__(self, client: AsyncClient, credentials: Mapping[str, str | None]) -> None:
        self.client = client
        self.credentials = credentials
        self.requests = 0

    async def post(
        self,
        body: Any,
        actor: str | None = "anonymous",
        *,
        headers: Mapping[str, str] | None = None,
        path: str = MCP_ENDPOINT,
    ) -> Response:
        self.requests += 1
        merged = {
            "accept": ACCEPT,
            "content-type": "application/json",
            **authorization(self.credentials, actor),
            **(headers or {}),
        }
        async with asyncio.timeout(CALL_TIMEOUT_SECONDS):
            return await self.client.post(path, json=body, headers=merged)

    async def call(
        self,
        tool: str,
        arguments: Mapping[str, Any] | None = None,
        actor: str | None = "anonymous",
        *,
        headers: Mapping[str, str] | None = None,
    ) -> ToolOutcome:
        body = {
            "jsonrpc": "2.0",
            "id": self.requests + 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": dict(arguments or {})},
        }
        return parse_tool_outcome(await self.post(body, actor, headers=headers))

    async def upload(
        self,
        data: bytes,
        actor: str | None = "owner",
        *,
        version: str | None = None,
        publish: bool = False,
    ) -> ToolOutcome:
        arguments: dict[str, Any] = {
            "content_base64": base64.b64encode(data).decode("ascii"),
            "publish": publish,
        }
        if version is not None:
            arguments["version"] = version
        return await self.call("upload_package", arguments, actor)


@dataclass(frozen=True)
class Outcome:
    code: str | None
    status: int


def rest_outcome(response: Response) -> Outcome:
    if response.status_code < 300:
        return Outcome(None, response.status_code)
    return Outcome(response.json()["error"]["code"], response.status_code)


def mcp_outcome(result: ToolOutcome) -> Outcome:
    return Outcome(result.code, result.status)


def differences(rest: Any, mcp: Any, path: str = "$") -> list[str]:
    if isinstance(rest, dict) and isinstance(mcp, dict):
        found: list[str] = []
        for key in sorted(set(rest) | set(mcp)):
            if key not in mcp:
                found.append(f"{path}.{key}: only REST has it ({rest[key]!r})")
            elif key not in rest:
                found.append(f"{path}.{key}: only MCP has it ({mcp[key]!r})")
            else:
                found.extend(differences(rest[key], mcp[key], f"{path}.{key}"))
        return found
    if isinstance(rest, list) and isinstance(mcp, list):
        if len(rest) != len(mcp):
            return [f"{path}: REST has {len(rest)} items, MCP has {len(mcp)}"]
        found = []
        for index, (left, right) in enumerate(zip(rest, mcp, strict=True)):
            found.extend(differences(left, right, f"{path}[{index}]"))
        return found
    if type(rest) is not type(mcp) or rest != mcp:
        return [f"{path}: REST {rest!r} != MCP {mcp!r}"]
    return []


def path_of_url(url: str) -> str:
    parts = urlsplit(url)
    return parts.path + (f"?{parts.query}" if parts.query else "")
