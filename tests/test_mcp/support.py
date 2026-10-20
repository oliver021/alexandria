from __future__ import annotations

import asyncio
import base64
import json
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import httpx2
from jose import jwt
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.local import LocalAuthProvider
from app.models.schemas import TokenCreate
from app.repositories import user_repo
from app.services.package_service import PackageService
from app.services.principal import Principal
from app.services.skill_service import SkillService
from app.services.token_service import TokenService
from app.services.version_service import VersionService
from app.storage import BlobStore
from tests.helpers.factories import Actors, upload_package

SECRET = "mcp-test-secret-0123456789abcdef"
BASE_URL = "http://test"
MCP_PATH = "/mcp"
ACCEPT = "application/json, text/event-stream"
CLIENT_TIMEOUT = 30

PDF = "pdf-forms"
API = "api-reference"
DRAFT = "draft-only"
HIDDEN = "hidden-one"
FOREIGN = "bobs-ocr"

GUIDE_TEXT = "# Guide\n\nFill every field before flattening.\n"
SCRIPT_TEXT = "print('fill')\n"
BINARY_BYTES = bytes(range(256))
LONG_TEXT = "0123456789abcdef" * 200
UNICODE_PATH = "docs/ñandú.md"
UNICODE_TEXT = "contenido con ñ y acentos: ÁÉÍÓÚ\n"

PDF_FILES: dict[str, str | bytes] = {
    "docs/guide.md": GUIDE_TEXT,
    "docs/long.txt": LONG_TEXT,
    "scripts/fill.py": SCRIPT_TEXT,
    "data/table.bin": BINARY_BYTES,
    UNICODE_PATH: UNICODE_TEXT,
}
API_FILES: dict[str, str | bytes] = {
    "tables/codes.csv": "code,meaning\n200,ok\n404,missing\n",
    "schemas/api.json": '{"openapi": "3.1.0"}\n',
}


@dataclass(frozen=True)
class Registry:
    pdf_id: str
    api_id: str
    draft_id: str
    hidden_id: str
    foreign_id: str


@dataclass(frozen=True)
class Credentials:
    owner_read: str
    owner_write: str
    owner_jwt: str
    stranger_read: str
    stranger_write: str
    admin_read: str
    admin_write: str


async def seed_registry(
    session_factory: async_sessionmaker[AsyncSession], store: BlobStore, actors: Actors
) -> Registry:
    async with session_factory() as db:
        packages = PackageService(db, store)
        skills = SkillService(db)
        versions = VersionService(db)
        owner, stranger = actors.owner, actors.stranger
        for number in ("0.9.0", "1.0.0", "1.1.0", "2.0.0-beta.1"):
            await upload_package(
                packages,
                owner,
                PDF,
                number,
                description="Fill and flatten PDF forms",
                tags=["pdf", "forms"],
                category="documents",
                files=PDF_FILES,
            )
        await upload_package(
            packages,
            owner,
            PDF,
            "2.0.0",
            publish=False,
            description="Fill and flatten PDF forms",
            tags=["pdf", "forms"],
            category="documents",
            files=PDF_FILES,
        )
        await versions.deprecate(owner, PDF, "0.9.0")
        await upload_package(
            packages,
            owner,
            API,
            "1.0.0",
            kind="grounding",
            description="Reference for the HTTP API error codes",
            tags=["api", "reference"],
            category="reference",
            files=API_FILES,
        )
        await upload_package(packages, owner, DRAFT, "0.1.0", publish=False)
        await upload_package(packages, owner, HIDDEN, "1.0.0")
        await skills.hide(owner, HIDDEN)
        await upload_package(
            packages,
            stranger,
            FOREIGN,
            "1.0.0",
            description="Read text out of scanned pages",
            tags=["pdf", "ocr"],
            category="documents",
        )
        pdf = await skills.get(owner, PDF)
        api = await skills.get(owner, API)
        draft = await skills.get(owner, DRAFT)
        hidden = await skills.get(owner, HIDDEN)
        foreign = await skills.get(stranger, FOREIGN)
    return Registry(
        pdf_id=pdf.id,
        api_id=api.id,
        draft_id=draft.id,
        hidden_id=hidden.id,
        foreign_id=foreign.id,
    )


def forge_jwt(
    subject: object,
    *,
    secret: str = SECRET,
    algorithm: str = "HS256",
    lifetime: timedelta = timedelta(minutes=5),
    with_expiry: bool = True,
) -> str:
    payload: dict[str, Any] = {}
    if subject is not None:
        payload["sub"] = subject
    if with_expiry:
        payload["exp"] = datetime.now(UTC) + lifetime
    return jwt.encode(payload, secret, algorithm=algorithm)


def b64url(data: Mapping[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode("utf-8")).rstrip(b"=").decode("ascii")


def unsigned_jwt(subject: str) -> str:
    expiry = int((datetime.now(UTC) + timedelta(minutes=5)).timestamp())
    header = b64url({"alg": "none", "typ": "JWT"})
    return f"{header}.{b64url({'sub': subject, 'exp': expiry})}."


def hostile_jwts(user_id: str, admin_id: str) -> dict[str, str]:
    valid = forge_jwt(user_id)
    header, payload, signature = valid.split(".")
    middle = len(signature) // 2
    flipped = "A" if signature[middle] != "A" else "B"
    expiry = int((datetime.now(UTC) + timedelta(minutes=5)).timestamp())
    return {
        "expired": forge_jwt(user_id, lifetime=timedelta(minutes=-1)),
        "foreign-secret": forge_jwt(user_id, secret="another-secret-entirely"),
        "alg-none": unsigned_jwt(user_id),
        "hs512": forge_jwt(user_id, algorithm="HS512"),
        "no-subject": forge_jwt(None),
        "no-expiry": forge_jwt(user_id, with_expiry=False),
        "numeric-subject": forge_jwt(12345),
        "unknown-user": forge_jwt(str(uuid.uuid4())),
        "tampered-signature": f"{header}.{payload}.{signature[:middle]}{flipped}{signature[middle + 1 :]}",
        "swapped-payload": f"{header}.{b64url({'sub': admin_id, 'exp': expiry})}.{signature}",
        "truncated": valid[:-10],
        "extra-segment": f"{valid}.extra",
    }


async def issue_credentials(
    session_factory: async_sessionmaker[AsyncSession], actors: Actors
) -> Credentials:
    async with session_factory() as db:
        tokens = TokenService(db)

        async def issue(principal: Principal, scope: str) -> str:
            created = await tokens.create(
                principal, TokenCreate(name=f"{scope}-token", scope=scope)
            )
            return created.token

        owner_user = await user_repo.get_user(db, actors.owner.user_id)
        assert owner_user is not None
        owner_jwt = await LocalAuthProvider(secret=SECRET).create_token(owner_user)
        return Credentials(
            owner_read=await issue(actors.owner, "read"),
            owner_write=await issue(actors.owner, "write"),
            owner_jwt=owner_jwt,
            stranger_read=await issue(actors.stranger, "read"),
            stranger_write=await issue(actors.stranger, "write"),
            admin_read=await issue(actors.admin, "read"),
            admin_write=await issue(actors.admin, "write"),
        )


@asynccontextmanager
async def open_client(
    app: Any,
    token: str | None = None,
    *,
    mode: str = "legacy",
    base_url: str = BASE_URL,
    path: str = MCP_PATH,
    headers: Mapping[str, str] | None = None,
) -> AsyncIterator[Client]:
    request_headers = dict(headers or {})
    if token is not None:
        request_headers["Authorization"] = f"Bearer {token}"
    async with asyncio.timeout(CLIENT_TIMEOUT):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url=base_url, headers=request_headers
        ) as http:
            transport = streamable_http_client(f"{base_url}{path}", http_client=http)
            async with Client(transport, mode=mode) as client:
                yield client


def rpc(method: str, params: Mapping[str, Any] | None = None, request_id: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params or {})}


def rpc_call(name: str, arguments: Mapping[str, Any] | None = None) -> dict:
    return rpc("tools/call", {"name": name, "arguments": dict(arguments or {})})


async def post(
    app: Any,
    body: Any = None,
    *,
    token: str | None = None,
    headers: Mapping[str, str] | list[tuple[Any, Any]] | None = None,
    path: str = MCP_PATH,
    base_url: str = BASE_URL,
    method: str = "POST",
    content: bytes | AsyncIterator[bytes] | None = None,
) -> httpx.Response:
    defaults: dict[Any, Any] = {"accept": ACCEPT, "content-type": "application/json"}
    extra: list[tuple[Any, Any]] = []
    if isinstance(headers, Mapping):
        defaults.update(headers)
    elif headers is not None:
        extra.extend(headers)
    if token is not None:
        extra.append(("authorization", f"Bearer {token}"))
    request_headers = [*defaults.items(), *extra]
    async with asyncio.timeout(CLIENT_TIMEOUT):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=base_url
        ) as http:
            if content is not None:
                return await http.request(method, path, content=content, headers=request_headers)
            return await http.request(method, path, json=body, headers=request_headers)


def text_of(result: CallToolResult) -> str:
    assert len(result.content) == 1
    block = result.content[0]
    assert block.type == "text"
    return block.text


def payload_of(result: CallToolResult) -> Any:
    return json.loads(text_of(result))


def zip_to_base64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")
