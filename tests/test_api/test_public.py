from __future__ import annotations

import ast
import asyncio
import hashlib
import io
import itertools
import json
import logging
import random
import re
import shlex
import sys
import threading
import time
import tokenize
import zipfile
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import parse_qsl, quote, unquote, unquote_plus, urlencode, urlsplit

import jinja2
import pytest
import pytest_asyncio
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from httpx import ASGITransport, AsyncClient, Response
from markdown_it.rules_core import StateCore
from markdown_it.token import Token
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api import public
from app.config.settings import settings
from app.db.session import get_db
from app.models import Skill, SkillVersion
from app.models.schemas import ManifestEntryResponse
from app.repositories import skill_repo
from app.services.errors import (
    ConflictError,
    DomainError,
    ForbiddenError,
    InvalidPackageError,
    NotFoundError,
    StorageError,
    UnauthorizedError,
    ValidationError,
)
from app.services.package_service import PackageService
from app.services.principal import Principal
from app.services.skill_service import SkillService
from app.services.version_service import VersionService
from app.storage import LocalBlobStore, get_blob_store
from tests.helpers.factories import add_skill, upload_package

CSP = (
    "default-src 'none'; style-src 'self'; img-src 'self' data:; script-src 'self'; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
PUBLIC_URL = "https://registry.example.org"
ELLIPSIS = chr(0x2026)
MIDDLE_DOT = chr(0xB7)
EN_DASH = chr(0x2013)
VOID_TAGS = frozenset({"meta", "link", "input", "br", "hr", "img", "source", "base", "area", "col"})
FORBIDDEN_TAGS = frozenset(
    {
        "img",
        "iframe",
        "frame",
        "frameset",
        "object",
        "embed",
        "applet",
        "style",
        "base",
        "audio",
        "video",
        "source",
        "track",
        "canvas",
        "template",
        "noscript",
        "portal",
        "marquee",
    }
)
URL_ATTRIBUTES = frozenset(
    {"href", "src", "action", "formaction", "data", "poster", "srcset", "xlink:href", "ping"}
)
SAFE_URL_PREFIXES = ("/", "#", "?", "http://", "https://", "mailto:")
MARKDOWN_TAGS = frozenset(
    {
        "p",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "ul",
        "ol",
        "li",
        "strong",
        "em",
        "s",
        "code",
        "pre",
        "blockquote",
        "hr",
        "br",
        "a",
        "span",
        "table",
        "thead",
        "tbody",
        "tr",
        "th",
        "td",
        "div",
    }
)


class Node:
    def __init__(self, tag: str, attrs: dict[str, str | None], parent: Node | None = None) -> None:
        self.tag = tag
        self.attrs = attrs
        self.parent = parent
        self.children: list[Node | str] = []

    def walk(self) -> Iterator[Node]:
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def text(self) -> str:
        return "".join(child if isinstance(child, str) else child.text() for child in self.children)

    def words(self) -> str:
        return " ".join(self.text().split())

    @property
    def classes(self) -> list[str]:
        return (self.attrs.get("class") or "").split()

    def find_all(
        self, tag: str | None = None, *, id: str | None = None, cls: str | None = None, **attrs: str
    ) -> list[Node]:
        found = []
        for node in self.walk():
            if node is self or (tag is not None and node.tag != tag):
                continue
            if id is not None and node.attrs.get("id") != id:
                continue
            if cls is not None and cls not in node.classes:
                continue
            if any(node.attrs.get(key.replace("_", "-")) != value for key, value in attrs.items()):
                continue
            found.append(node)
        return found

    def find(self, tag: str | None = None, **criteria: Any) -> Node:
        found = self.find_all(tag, **criteria)
        assert found, f"no element matches {tag!r} {criteria!r}"
        return found[0]

    def by_id(self, identifier: str) -> Node:
        return self.find(id=identifier)


class Document(Node):
    def __init__(self, response: Response | None = None) -> None:
        super().__init__("#document", {})
        self.response = response
        self.problems: list[str] = []


class Builder(HTMLParser):
    def __init__(self, document: Document) -> None:
        super().__init__(convert_charrefs=True)
        self.document = document
        self.current: Node = document

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, dict(attrs), self.current)
        self.current.children.append(node)
        if tag not in VOID_TAGS:
            self.current = node

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.current.children.append(Node(tag, dict(attrs), self.current))

    def handle_endtag(self, tag: str) -> None:
        node = self.current
        while node is not self.document and node.tag != tag:
            assert node.parent is not None
            node = node.parent
        if node is self.document:
            self.document.problems.append(f"stray </{tag}>")
            return
        if node is not self.current:
            self.document.problems.append(f"</{tag}> closes unclosed <{self.current.tag}>")
        assert node.parent is not None
        self.current = node.parent

    def handle_data(self, data: str) -> None:
        self.current.children.append(data)


def parse(source: str, response: Response | None = None) -> Document:
    document = Document(response)
    builder = Builder(document)
    builder.feed(source)
    builder.close()
    if builder.current is not document:
        document.problems.append(f"<{builder.current.tag}> is never closed")
    return document


def normalized_url(value: str) -> str:
    return re.sub(r"[\x00-\x20]+", "", value).lower()


def assert_inert(document: Node, *, allowed_tags: frozenset[str] | None = None) -> None:
    scripts = document.find_all("script")
    for script in scripts:
        source = (script.attrs.get("src") or "").split("?")[0]
        assert source == f"{public.STATIC_URL}/site.js", script.attrs
        assert not script.text().strip()
    for node in document.walk():
        if node is document:
            continue
        assert node.tag not in FORBIDDEN_TAGS, node.tag
        if allowed_tags is not None:
            assert node.tag in allowed_tags, node.tag
        if node.tag == "meta":
            assert "http-equiv" not in node.attrs
        if node.tag == "link":
            assert node.attrs.get("rel") in {"icon", "stylesheet", "canonical"}, node.attrs
        for name, value in node.attrs.items():
            assert not name.startswith("on"), (node.tag, name)
            assert name != "style", node.tag
            assert name not in {"srcdoc", "formaction"}, name
            if name in URL_ATTRIBUTES:
                assert value is not None
                flat = normalized_url(value)
                assert flat.startswith(SAFE_URL_PREFIXES), (node.tag, name, value)
                assert not flat.startswith(("javascript:", "vbscript:", "data:")), value


def assert_accessible(document: Document) -> None:
    html = document.find("html")
    assert html.attrs.get("lang") == "en"
    assert len(document.find_all("h1")) == 1
    assert document.find("main").attrs.get("id") == "main"
    assert document.find_all("a")[0].attrs.get("href") == "#main"
    levels = [int(node.tag[1]) for node in document.walk() if re.fullmatch(r"h[1-6]", node.tag)]
    assert levels[0] == 1
    assert all(later - earlier <= 1 for earlier, later in itertools.pairwise(levels)), levels
    identifiers = [node.attrs["id"] for node in document.walk() if "id" in node.attrs]
    assert len(identifiers) == len(set(identifiers))
    labels = {label.attrs.get("for") for label in document.find_all("label")}
    for control in (*document.find_all("input"), *document.find_all("select")):
        if control.attrs.get("type") == "hidden":
            continue
        assert control.attrs.get("id") in labels or control.attrs.get("aria-label"), control.attrs
    for anchor in document.find_all("a"):
        assert anchor.words() or anchor.attrs.get("aria-label"), anchor.attrs
    for button in document.find_all("button"):
        assert button.words() or button.attrs.get("aria-label")
    for icon in document.find_all("svg"):
        assert icon.attrs.get("aria-hidden") == "true"
    for navigation in document.find_all("nav"):
        assert navigation.attrs.get("aria-label")
    for header in document.find_all("th"):
        in_markdown = any("md-table" in parent.classes for parent in ancestors(header))
        assert in_markdown or header.attrs.get("scope") in {"col", "row"}
    for node in document.walk():
        target = node.attrs.get("aria-labelledby")
        if target:
            assert target in identifiers, target
    for stamp in document.find_all("time"):
        assert stamp.attrs.get("datetime")
    for details in document.find_all("details"):
        first = next(child for child in details.children if isinstance(child, Node))
        assert first.tag == "summary"


def ancestors(node: Node) -> Iterator[Node]:
    current = node.parent
    while current is not None:
        yield current
        current = current.parent


def assert_security_headers(response: Response) -> None:
    assert response.headers["content-security-policy"] == CSP
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["content-type"].startswith("text/html")


async def visit(
    client: AsyncClient, url: str, status: int = 200, *, method: str = "GET", checks: bool = True
) -> Document:
    response = await client.request(method, url)
    assert response.status_code == status, f"{url}: {response.status_code} {response.text[:300]}"
    assert_security_headers(response)
    document = parse(response.text if method != "HEAD" else "", response)
    if method != "HEAD" and checks:
        assert document.problems == [], document.problems
        assert_inert(document)
        assert_accessible(document)
    return document


class SpyStore:
    def __init__(self, inner: LocalBlobStore) -> None:
        self.inner = inner
        self.reads: list[str] = []

    async def put(self, key: str, data: bytes) -> bool:
        return await self.inner.put(key, data)

    async def get(self, key: str) -> bytes:
        self.reads.append(key)
        return await self.inner.get(key)

    async def open(self, key: str) -> BinaryIO:
        self.reads.append(key)
        return await self.inner.open(key)

    async def exists(self, key: str) -> bool:
        return await self.inner.exists(key)

    async def delete(self, key: str) -> bool:
        return await self.inner.delete(key)

    async def size(self, key: str) -> int:
        return await self.inner.size(key)

    def stream(self, key: str, chunk_size: int = 65536) -> AsyncIterator[bytes]:
        self.reads.append(key)
        return self.inner.stream(key, chunk_size)


@pytest.fixture(autouse=True)
def site_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings.server, "public_url", PUBLIC_URL)
    monkeypatch.setattr(settings.registry, "public_read", True)
    monkeypatch.setattr(settings.registry, "auto_publish", False)
    monkeypatch.setattr(settings.registry.limits, "max_inline_read_bytes", 262144)
    public.MARKDOWN_CACHE.clear()
    yield
    public.MARKDOWN_CACHE.clear()


@pytest.fixture
def spy(blob_store: LocalBlobStore) -> SpyStore:
    return SpyStore(blob_store)


@pytest_asyncio.fixture
async def site(
    session_factory: async_sessionmaker[AsyncSession], spy: SpyStore
) -> AsyncIterator[AsyncClient]:
    application = FastAPI()
    application.include_router(public.router)
    application.mount(
        "/static", StaticFiles(directory=public.ROOT / "web" / "static"), name="static"
    )

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_db] = override_db
    application.dependency_overrides[get_blob_store] = lambda: spy
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def publish(
    db: AsyncSession,
    store: LocalBlobStore,
    owner: Principal,
    name: str,
    version: str = "1.0.0",
    *,
    kind: str = "skill",
    live: bool = True,
    **options: Any,
) -> None:
    service = PackageService(db, store)
    await upload_package(service, owner, name, version, kind=kind, publish=live, **options)


async def touch(
    db: AsyncSession, name: str, *, updated: datetime | None = None, created: datetime | None = None
) -> None:
    values: dict[str, datetime] = {}
    if updated is not None:
        values["updated_at"] = updated
    if created is not None:
        values["created_at"] = created
    await db.execute(update(Skill).where(Skill.name == name).values(**values))
    await db.commit()


def card_names(document: Node) -> list[str]:
    return [
        card.find("a", cls="card__link").attrs["href"].rsplit("/", 1)[-1]
        for card in document.find_all("article", cls="card")
    ]


def facts(document: Node) -> dict[str, str]:
    result = {}
    for item in document.find_all("li"):
        labels = item.find_all("span", cls="facts__label")
        if labels:
            label = labels[0].words()
            result[label] = item.words().removeprefix(label).strip()
    return result


def by_name(document: Node) -> dict[str, Node]:
    return {
        card.find("a", cls="card__link").attrs["href"].rsplit("/", 1)[-1]: card
        for card in document.find_all("article", cls="card")
    }


async def test_the_landing_page_of_an_empty_registry(site):
    page = await visit(site, "/")

    assert page.find("h1").words() == "A versioned library of skills and grounding."
    items = page.find_all("div", cls="ledger__item")
    assert [item.find("dt").words() for item in items] == [
        "Skills",
        "Grounding",
        "Published entries",
    ]
    assert [item.find("dd").words() for item in items] == ["0", "0", "0"]
    assert not page.find_all("article", cls="card")
    assert not page.find_all("h2", id="recent-title")
    assert page.by_id("connect-url").words() == f"{PUBLIC_URL}/mcp"
    assert page.by_id("alx-pull").text().splitlines()[-1] == "alx pull my-skill"


async def test_the_landing_page_explains_what_alexandria_is(site):
    page = await visit(site, "/")

    text = page.words()
    assert "versioned library of skills and grounding" in text
    assert "immutable zip with a single entry point" in text
    skill = page.find("article", cls="kind--skill")
    assert "SKILL.md" in skill.words() and "procedural" in skill.words()
    grounding = page.find("article", cls="kind--grounding")
    assert "INDEX.md" in grounding.words() and "referential" in grounding.words()
    hrefs = {link.attrs["href"] for link in page.find_all("a")}
    assert {"/registry", "/docs", "/registry?kind=skill", "/registry?kind=grounding"} <= hrefs
    assert page.find("link", rel="canonical").attrs["href"] == f"{PUBLIC_URL}/"


async def test_the_landing_page_shows_how_to_connect_over_mcp(site):
    page = await visit(site, "/")

    assert page.by_id("connect-url").words() == "https://registry.example.org/mcp"
    assert page.by_id("connect-add").words() == (
        "claude mcp add --transport http alexandria https://registry.example.org/mcp"
    )
    call = shlex.split(page.by_id("connect-call").text().replace("\\\n", " "))
    assert call[:3] == ["curl", "-s", "https://registry.example.org/mcp"]
    assert "Accept: application/json, text/event-stream" in call
    request = json.loads(call[call.index("-d") + 1])
    assert request["method"] == "tools/call"
    assert request["params"]["name"] == "search_skills"
    tools = {code.words() for code in page.find("table", cls="tools").find_all("code")}
    assert {"search_skills", "get_entrypoint", "read_file", "get_download_info"} <= tools


async def test_the_landing_page_uses_the_configured_public_url(site, monkeypatch):
    monkeypatch.setattr(settings.server, "public_url", "http://alexandria.internal:9000")

    page = await visit(site, "/")

    assert page.by_id("connect-url").words() == "http://alexandria.internal:9000/mcp"
    assert "curl -s" in page.by_id("rest-search").text()
    assert "http://alexandria.internal:9000/api/v1/skills?q=pdf" in page.by_id("rest-search").text()
    assert page.by_id("alx-pull").text().splitlines()[0] == (
        "export ALX_URL=http://alexandria.internal:9000"
    )
    assert "http://alexandria.internal:9000/mcp" in page.find("footer").words()


async def test_the_landing_page_counts_only_published_entries_by_kind(
    site, db_session, blob_store, actors
):
    owner = actors.owner
    await publish(db_session, blob_store, owner, "skill-one")
    await publish(db_session, blob_store, owner, "skill-two")
    await publish(db_session, blob_store, owner, "ground-one", kind="grounding")
    await publish(db_session, blob_store, owner, "skill-draft", live=False)
    await publish(db_session, blob_store, owner, "ground-draft", kind="grounding", live=False)
    await publish(db_session, blob_store, owner, "skill-hidden")
    await SkillService(db_session).hide(owner, "skill-hidden")

    page = await visit(site, "/")

    counts = {
        item.find("dt").words(): item.find("dd").words()
        for item in page.find_all("div", cls="ledger__item")
    }
    assert counts == {"Skills": "2", "Grounding": "1", "Published entries": "3"}
    text = page.text()
    for name in ("skill-draft", "ground-draft", "skill-hidden"):
        assert name not in text


async def test_the_landing_page_lists_recent_entries_and_builds_examples_from_the_newest(
    site, db_session, blob_store, actors
):
    owner = actors.owner
    moment = datetime(2026, 5, 1, tzinfo=UTC)
    for index, name in enumerate(("oldest", "middle", "newest")):
        await publish(db_session, blob_store, owner, name)
        await touch(db_session, name, updated=moment + timedelta(days=index))
    await publish(db_session, blob_store, owner, "unfinished", live=False)

    page = await visit(site, "/")

    recent = page.find("section", **{"aria-labelledby": "recent-title"})
    assert card_names(recent) == ["newest", "middle", "oldest"]
    assert page.by_id("rest-entrypoint").words() == (
        "curl -s https://registry.example.org/api/v1/skills/newest/versions/latest/entrypoint"
    )
    assert page.by_id("alx-pull").text().splitlines()[-1] == "alx pull newest"
    assert page.find("a", cls="more", href="/registry?sort=updated")


async def test_the_landing_page_shows_at_most_six_recent_entries(
    site, db_session, blob_store, actors
):
    for index in range(8):
        await publish(db_session, blob_store, actors.owner, f"entry-{index}")

    page = await visit(site, "/")

    assert len(page.find_all("article", cls="card")) == public.RECENT_ENTRIES == 6


async def test_the_landing_page_of_a_private_registry_lists_nothing(
    site, db_session, blob_store, actors, monkeypatch
):
    await publish(
        db_session, blob_store, actors.owner, "hidden-treasure", description="Secret catalog entry"
    )
    monkeypatch.setattr(settings.registry, "public_read", False)

    page = await visit(site, "/")

    assert not page.find_all("dl", cls="ledger__grid")
    assert not page.find_all("article", cls="card")
    assert "This registry is private" in page.find("p", cls="notice--lock").words()
    assert "hidden-treasure" not in page.text()
    assert "Secret catalog entry" not in page.text()
    assert "-H" in page.by_id("rest-search").text()
    assert "Authorization: Bearer $ALX_TOKEN" in page.by_id("rest-search").text()
    assert "export ALX_TOKEN=" in page.by_id("alx-pull").text()
    assert "Authorization: Bearer $ALX_TOKEN" in page.by_id("connect-call").text()
    assert page.by_id("alx-pull").text().splitlines()[-1] == "alx pull my-skill"


async def test_head_requests_are_answered_with_the_same_headers(site):
    response = (await visit(site, "/", method="HEAD")).response

    assert response.status_code == 200
    assert response.content == b""
    assert (await visit(site, "/registry", method="HEAD")).response.status_code == 200


async def test_the_registry_page_of_an_empty_registry(site):
    page = await visit(site, "/registry")

    assert page.find("h1").words() == "Registry"
    assert page.by_id("results-title").words() == "0 entries"
    assert "Nothing has been published yet" in page.find("div", cls="empty").words()
    assert not page.find_all("article", cls="card")
    assert not page.find_all("nav", **{"aria-label": "Pagination"})
    assert not page.find_all("ul", cls="chips--active")


async def test_the_registry_page_with_a_trailing_slash_is_the_same_page(site):
    page = await visit(site, "/registry/")

    assert page.by_id("results-title").words() == "0 entries"


async def test_the_registry_lists_published_entries_sorted_by_name(
    site, db_session, blob_store, actors
):
    owner = actors.owner
    for name in ("zebra", "alpha", "middle"):
        await publish(db_session, blob_store, owner, name)
    await publish(db_session, blob_store, owner, "only-draft", live=False)
    await publish(db_session, blob_store, owner, "gone", live=True)
    await SkillService(db_session).hide(owner, "gone")

    page = await visit(site, "/registry")

    assert card_names(page) == ["alpha", "middle", "zebra"]
    assert page.by_id("results-title").words() == "3 entries"
    assert page.find("p", cls="results__range").words() == f"Showing 1{EN_DASH}3"
    assert "only-draft" not in page.text() and "gone" not in page.text()


async def test_a_card_shows_name_kind_description_tags_and_latest_version(
    site, db_session, blob_store, actors
):
    long_description = " ".join(["word"] * 120)
    await publish(
        db_session,
        blob_store,
        actors.owner,
        "pdf-tools",
        "1.4.2",
        display_name="PDF Tools",
        description=long_description,
        tags=["pdf", "docs"],
        category="documents",
    )
    await publish(
        db_session, blob_store, actors.owner, "style-guide", kind="grounding", description="Style."
    )

    page = await visit(site, "/registry")

    card = by_name(page)["pdf-tools"]
    link = card.find("a", cls="card__link")
    assert link.attrs["href"] == "/registry/skills/pdf-tools"
    assert link.words() == "PDF Tools"
    assert card.find("code").words() == "pdf-tools"
    assert card.find("span", cls="badge--skill").words() == "Skill"
    assert card.find("span", cls="version").words() == "v1.4.2"
    description = card.find("p", cls="card__description").words()
    assert description.endswith(ELLIPSIS)
    assert 150 < len(description) <= 220
    chips = {
        chip.words(): chip.attrs["href"] for chip in card.find("ul", cls="chips").find_all("a")
    }
    assert chips == {
        "documents": "/registry?category=documents",
        "pdf": "/registry?tag=pdf",
        "docs": "/registry?tag=docs",
    }
    assert datetime.fromisoformat(card.find("time").attrs["datetime"]).year >= 2026
    other = by_name(page)["style-guide"]
    assert other.find("span", cls="badge--grounding").words() == "Grounding"
    assert "card--grounding" in other.classes


async def test_cards_link_to_the_name_and_never_expose_the_uuid(
    site, db_session, blob_store, actors
):
    await publish(db_session, blob_store, actors.owner, "pdf-tools")
    entry = await SkillService(db_session).get(None, "pdf-tools")

    page = await visit(site, "/registry")

    assert entry.id not in page.response.text
    hrefs = [link.attrs["href"] for link in page.find_all("a")]
    assert "/registry/skills/pdf-tools" in hrefs


@pytest_asyncio.fixture
async def library(db_session, blob_store, actors) -> None:
    owner = actors.owner
    store = blob_store
    await publish(
        db_session,
        store,
        owner,
        "pdf-tools",
        description="Extract text from PDF files.",
        tags=["pdf", "docs"],
        category="documents",
    )
    await publish(
        db_session,
        store,
        owner,
        "sql-style",
        kind="grounding",
        description="Format SQL queries.",
        tags=["sql", "docs"],
        category="data",
    )
    await publish(
        db_session,
        store,
        owner,
        "api-design",
        description="Design HTTP interfaces.",
        tags=["rest", "http"],
        category="engineering",
    )
    await publish(
        db_session,
        store,
        owner,
        "data-lake",
        kind="grounding",
        description="Reference tables.",
        tags=["sql", "data"],
        category="data",
    )


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("", ["api-design", "data-lake", "pdf-tools", "sql-style"]),
        ("?kind=skill", ["api-design", "pdf-tools"]),
        ("?kind=grounding", ["data-lake", "sql-style"]),
        ("?kind=GROUNDING", ["data-lake", "sql-style"]),
        ("?tag=docs", ["pdf-tools", "sql-style"]),
        ("?tag=docs&tag=sql", ["sql-style"]),
        ("?tag=sql&kind=skill", []),
        ("?category=data", ["data-lake", "sql-style"]),
        ("?category=data&tag=docs", ["sql-style"]),
        ("?q=format", ["sql-style"]),
        ("?q=PDF", ["pdf-tools"]),
        ("?q=sql", ["sql-style", "data-lake"]),
        ("?q=sql&kind=grounding&category=data", ["sql-style", "data-lake"]),
        ("?q=interfaces", ["api-design"]),
        ("?q=nothing-matches-this", []),
        ("?q=", ["api-design", "data-lake", "pdf-tools", "sql-style"]),
        ("?kind=&category=&sort=", ["api-design", "data-lake", "pdf-tools", "sql-style"]),
    ],
)
async def test_the_registry_filters_by_search_kind_tag_and_category(site, library, query, expected):
    page = await visit(site, f"/registry{query}")

    assert sorted(card_names(page)) == sorted(expected)
    assert page.by_id("results-title").words() == (
        f"{len(expected)} {'entry' if len(expected) == 1 else 'entries'}"
    )


async def test_an_empty_result_offers_to_clear_the_filters(site, library):
    page = await visit(site, "/registry?q=zzz&tag=pdf")

    empty = page.find("div", cls="empty")
    assert "No entries match these filters" in empty.words()
    assert empty.find("a", href="/registry")
    assert "Nothing has been published yet" not in page.text()


async def test_the_search_form_keeps_the_filters_and_the_query(site, library):
    page = await visit(site, "/registry?q=sql&kind=grounding&category=data&tag=docs&tag=sql")

    form = page.find("form", role="search")
    assert form.attrs["method"] == "get" and form.attrs["action"] == "/registry"
    assert page.by_id("q").attrs["value"] == "sql"
    hidden = [(i.attrs["name"], i.attrs["value"]) for i in form.find_all("input", type="hidden")]
    assert hidden == [
        ("kind", "grounding"),
        ("category", "data"),
        ("tag", "docs"),
        ("tag", "sql"),
    ]
    assert page.find("label", **{"for": "q"})
    assert page.find("label", **{"for": "sort"})


async def test_the_active_filters_can_be_removed_one_by_one(site, library):
    page = await visit(site, "/registry?q=sql&kind=grounding&category=data&tag=docs&tag=sql")

    chips = {
        chip.words().removesuffix("(remove filter)").strip(): chip.attrs["href"]
        for chip in page.find("ul", cls="chips--active").find_all("a", cls="chip")
    }
    assert chips == {
        "Search: sql": "/registry?kind=grounding&category=data&tag=docs&tag=sql",
        "Kind: grounding": "/registry?q=sql&category=data&tag=docs&tag=sql",
        "Category: data": "/registry?q=sql&kind=grounding&tag=docs&tag=sql",
        "Tag: docs": "/registry?q=sql&kind=grounding&category=data&tag=sql",
        "Tag: sql": "/registry?q=sql&kind=grounding&category=data&tag=docs",
    }
    assert page.find("a", cls="clear").attrs["href"] == "/registry"


async def test_the_facets_show_kinds_categories_and_tags_with_counts(site, library):
    page = await visit(site, "/registry")

    kinds = page.by_id("facet-kind").parent
    assert [a.words() for a in kinds.find_all("a")] == ["All kinds", "Skill", "Grounding"]
    assert kinds.find("a", **{"aria-current": "true"}).words() == "All kinds"
    assert [a.attrs["href"] for a in kinds.find_all("a")] == [
        "/registry",
        "/registry?kind=skill",
        "/registry?kind=grounding",
    ]
    categories = [a.words() for a in page.by_id("facet-category").parent.find_all("a")]
    assert categories == ["data 2", "documents 1", "engineering 1"]
    tags = {
        a.find("span", cls="chip__label").words(): (
            a.find("span", cls="count").words(),
            a.attrs["href"],
        )
        for a in page.by_id("facet-tags").parent.find_all("a")
    }
    assert tags == {
        "docs": ("2", "/registry?tag=docs"),
        "sql": ("2", "/registry?tag=sql"),
        "data": ("1", "/registry?tag=data"),
        "http": ("1", "/registry?tag=http"),
        "pdf": ("1", "/registry?tag=pdf"),
        "rest": ("1", "/registry?tag=rest"),
    }


async def test_the_facet_links_combine_with_the_active_filters(site, library):
    page = await visit(site, "/registry?kind=grounding&tag=sql&sort=updated&page=1")

    by_label = {
        a.find("span", cls="chip__label").words(): a
        for a in page.by_id("facet-tags").parent.find_all("a")
    }
    assert set(by_label) == {"sql", "data", "docs"}
    assert by_label["sql"].attrs["aria-current"] == "true"
    assert by_label["sql"].attrs["href"] == "/registry?kind=grounding&sort=updated"
    docs = by_label["docs"]
    assert docs.attrs["href"] == "/registry?kind=grounding&tag=sql&tag=docs&sort=updated"
    assert "aria-current" not in docs.attrs
    categories = page.by_id("facet-category").parent.find_all("a")
    assert [(a.words(), a.attrs["href"]) for a in categories] == [
        ("data 2", "/registry?kind=grounding&category=data&tag=sql&sort=updated")
    ]
    kinds = page.by_id("facet-kind").parent.find_all("a")
    assert kinds[1].attrs["href"] == "/registry?kind=skill&tag=sql&sort=updated"
    assert kinds[2].attrs["aria-current"] == "true"
    assert kinds[2].attrs["href"] == "/registry?kind=grounding&tag=sql&sort=updated"


async def test_an_active_tag_outside_the_facet_list_stays_visible_and_removable(
    site, db_session, blob_store, actors
):
    await publish(db_session, blob_store, actors.owner, "plain", tags=["common"])
    for index in range(public.TAG_FACETS + 3):
        await publish(
            db_session,
            blob_store,
            actors.owner,
            f"tagged-{index:02d}",
            tags=[f"filler{index:02d}", "common"],
        )
    await publish(db_session, blob_store, actors.owner, "rare-one", tags=["zzz-rare"])

    page = await visit(site, "/registry?tag=zzz-rare")

    facets = {
        a.find("span", cls="chip__label").words(): a
        for a in page.by_id("facet-tags").parent.find_all("a")
    }
    assert len(facets) == public.TAG_FACETS + 1
    assert facets["zzz-rare"].attrs["aria-current"] == "true"
    assert facets["zzz-rare"].attrs["href"] == "/registry"
    assert card_names(page) == ["rare-one"]


async def test_the_sort_selector_offers_relevance_only_with_a_query(site, library):
    plain = await visit(site, "/registry")
    searching = await visit(site, "/registry?q=sql")

    def options(page: Node) -> list[tuple[str, bool]]:
        return [
            (option.attrs["value"], "selected" in option.attrs)
            for option in page.by_id("sort").find_all("option")
        ]

    assert options(plain) == [("name", True), ("updated", False), ("created", False)]
    assert options(searching) == [
        ("relevance", True),
        ("name", False),
        ("updated", False),
        ("created", False),
    ]
    chosen = await visit(site, "/registry?sort=created")
    assert options(chosen) == [("name", False), ("updated", False), ("created", True)]
    assert "data-autosubmit" in plain.by_id("sort").attrs


async def test_the_registry_sorts_by_relevance_name_updated_and_created(
    site, db_session, blob_store, actors
):
    for name in ("sql", "sql-style", "postgres-sql-notes", "formatter"):
        await publish(
            db_session,
            blob_store,
            actors.owner,
            name,
            description="Formats SQL." if name == "formatter" else "Plain description.",
        )
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for index, name in enumerate(("sql-style", "formatter", "sql", "postgres-sql-notes")):
        await touch(
            db_session,
            name,
            updated=base + timedelta(days=index),
            created=base - timedelta(days=index),
        )

    relevance = await visit(site, "/registry?q=sql")
    by_name_order = await visit(site, "/registry?q=sql&sort=name")
    updated = await visit(site, "/registry?sort=updated")
    created = await visit(site, "/registry?sort=created")

    assert card_names(relevance) == ["sql", "sql-style", "postgres-sql-notes", "formatter"]
    assert card_names(by_name_order) == ["formatter", "postgres-sql-notes", "sql", "sql-style"]
    assert card_names(updated) == ["postgres-sql-notes", "sql", "formatter", "sql-style"]
    assert card_names(created) == ["sql-style", "formatter", "sql", "postgres-sql-notes"]


@pytest_asyncio.fixture
async def bulk(db_session, actors) -> None:
    for index in range(45):
        await add_skill(db_session, actors.owner, f"bulk-{index:02d}", latest_version="1.0.0")


async def test_the_registry_paginates_by_twenty(site, bulk):
    first = await visit(site, "/registry")
    second = await visit(site, "/registry?page=2")
    third = await visit(site, "/registry?page=3")

    assert first.by_id("results-title").words() == "45 entries"
    assert first.find("p", cls="results__range").words() == f"Showing 1{EN_DASH}20"
    assert card_names(first) == [f"bulk-{i:02d}" for i in range(20)]
    assert card_names(second) == [f"bulk-{i:02d}" for i in range(20, 40)]
    assert second.find("p", cls="results__range").words() == f"Showing 21{EN_DASH}40"
    assert card_names(third) == [f"bulk-{i:02d}" for i in range(40, 45)]
    assert third.find("p", cls="results__range").words() == f"Showing 41{EN_DASH}45"


async def test_the_pagination_links_keep_the_filters(site, bulk):
    page = await visit(site, "/registry?kind=skill&page=2")

    pager = page.find("nav", **{"aria-label": "Pagination"})
    assert pager.find("a", rel="prev").attrs["href"] == "/registry?kind=skill"
    assert pager.find("a", rel="next").attrs["href"] == "/registry?kind=skill&page=3"
    current = pager.find("a", cls="is-current")
    assert current.attrs["aria-current"] == "page" and current.words() == "Page 2"
    assert [a.attrs["href"] for a in pager.find("ol").find_all("a")] == [
        "/registry?kind=skill",
        "/registry?kind=skill&page=2",
        "/registry?kind=skill&page=3",
    ]


async def test_the_first_and_last_pages_disable_one_step(site, bulk):
    first = await visit(site, "/registry")
    last = await visit(site, "/registry?page=3")

    assert not first.find_all("a", rel="prev")
    assert first.find("span", cls="is-disabled").words() == "Previous"
    assert not last.find_all("a", rel="next")
    assert last.find("span", cls="is-disabled").words() == "Next"
    assert not (await visit(site, "/registry?kind=grounding")).find_all("nav", cls="pager")


async def test_a_page_past_the_end_explains_itself_and_links_to_the_last_page(site, bulk):
    page = await visit(site, "/registry?page=9")

    empty = page.find("div", cls="empty")
    assert "nothing on this page" in empty.words() and "page 3" in empty.words()
    assert empty.find("a").attrs["href"] == "/registry?page=3"
    assert page.find("a", rel="prev").attrs["href"] == "/registry?page=3"
    assert not page.find_all("a", rel="next")
    assert not page.find_all("article", cls="card")


@pytest.mark.parametrize("page", ["0", "-1", "abc", "1.5", "10001", "99999999999999999999", ""])
async def test_an_invalid_page_number_is_a_bad_request(site, page):
    document = await visit(site, f"/registry?page={page}", 400)

    assert document.find("h1").words() == "Invalid request"
    assert "page" in document.find("p", cls="lead").words()


def test_the_window_of_page_numbers_collapses_long_ranges():
    assert public.page_numbers(1, 1) == [1]
    assert public.page_numbers(1, 5) == [1, 2, 3, 4, 5]
    assert public.page_numbers(1, 10) == [1, 2, 3, None, 10]
    assert public.page_numbers(5, 10) == [1, 2, 3, 4, 5, 6, 7, None, 10]
    assert public.page_numbers(7, 10) == [1, None, 5, 6, 7, 8, 9, 10]
    assert public.page_numbers(10, 10) == [1, None, 8, 9, 10]
    assert public.page_numbers(6, 8) == [1, None, 4, 5, 6, 7, 8]


@pytest.mark.parametrize(
    ("query", "message"),
    [
        ("kind=bogus", "Invalid kind 'bogus'"),
        ("sort=bogus", "Invalid sort 'bogus'"),
        ("q=" + "a" * 513, "q must have at most 512 characters"),
        ("q=a%00b", "NUL"),
        ("tag=" + "t" * 300, "tag must have at most 256 characters"),
        ("category=" + "c" * 300, "category must have at most 256 characters"),
    ],
)
async def test_invalid_filters_are_a_bad_request_with_a_readable_page(site, query, message):
    page = await visit(site, f"/registry?{query}", 400)

    assert page.find("h1").words() == "Invalid request"
    assert message in page.find("p", cls="lead").words()
    assert page.find("meta", name="robots").attrs["content"] == "noindex"
    assert not page.find_all("article", cls="card")


def tag_query(count: int, *, prefix: str = "t") -> str:
    return "&".join(f"tag={prefix}{index}" for index in range(count))


def test_every_site_route_that_needs_the_database_takes_its_session_with_function_scope():
    routes = [route for route in public.router.routes if isinstance(route, APIRoute)]
    sessions = [
        (route.path, dependency.scope)
        for route in routes
        for dependency in route.dependant.dependencies
        if dependency.call is get_db
    ]

    assert len(sessions) == 5
    assert {scope for _, scope in sessions} == {"function"}


def test_the_site_limit_on_tag_filters_is_the_one_of_the_search():
    assert public.MAX_TAG_FILTERS == skill_repo.MAX_TAG_FILTERS == 20


async def test_twenty_tag_filters_are_accepted_and_every_chip_drops_only_its_own_tag(site, library):
    tags = [f"tag{index}" for index in range(public.MAX_TAG_FILTERS)]

    page = await visit(site, f"/registry?{tag_query(public.MAX_TAG_FILTERS, prefix='tag')}")

    chips = page.find("ul", cls="chips--active").find_all("a", cls="chip")
    assert len(chips) == public.MAX_TAG_FILTERS
    for tag, chip in zip(tags, chips, strict=True):
        kept = [
            value for key, value in parse_qsl(urlsplit(chip.attrs["href"]).query) if key == "tag"
        ]
        assert kept == [other for other in tags if other != tag]
    assert page.find("a", cls="clear").attrs["href"] == "/registry"


@pytest.mark.parametrize(
    ("query", "message"),
    [
        (tag_query(public.MAX_TAG_FILTERS + 1), "at most 20 items"),
        ("&".join(["tag=same"] * (public.MAX_TAG_FILTERS + 1)), "at most 20 items"),
        ("&".join(["tag="] * 600), "at most 20 items"),
        (tag_query(5000), f"at most {public.MAX_QUERY_STRING} characters"),
    ],
    ids=["twenty-one", "twenty-one-equal", "hundreds-of-empty", "thousands"],
)
async def test_more_than_twenty_tag_filters_are_a_small_bad_request_page(
    site, library, query, message
):
    started = time.perf_counter()
    page = await visit(site, f"/registry?{query}", 400)
    elapsed = time.perf_counter() - started

    assert page.find("h1").words() == "Invalid request"
    assert message in page.find("p", cls="lead").words()
    assert not page.find_all("article", cls="card")
    assert len(page.response.content) < 16 * 1024
    assert elapsed < 0.5


@pytest.mark.parametrize("count", [100, 1000, 6000])
async def test_a_flood_of_tag_filters_never_builds_a_big_page(site, library, count):
    started = time.perf_counter()
    response = await site.get(f"/registry?{tag_query(count)}")
    elapsed = time.perf_counter() - started

    assert response.status_code == 400
    assert len(response.content) < 16 * 1024
    assert elapsed < 0.5


@pytest.mark.parametrize("name", ["q", "kind", "category", "sort", "page", "version", "unknown"])
async def test_a_repeated_parameter_cannot_inflate_the_site_either(site, library, name):
    value = {"kind": "skill", "sort": "name", "page": "1"}.get(name, "sql")
    pair = f"{name}={value}"
    fits = public.MAX_QUERY_STRING // (len(pair) + 1) - 1

    flooded = await site.get(f"/registry?{'&'.join([pair] * 5000)}")
    repeated = await site.get(f"/registry?{'&'.join([pair] * fits)}")

    assert flooded.status_code == 400
    assert len(flooded.content) < 16 * 1024
    assert repeated.status_code == 200
    assert len(repeated.content) < 100 * 1024


async def test_the_query_string_limit_is_exact_and_covers_every_page_of_the_site(site, showcase):
    padding = "x" * (public.MAX_QUERY_STRING - len("pad="))
    paths = [
        "/",
        "/registry",
        "/registry/skills/pdf-tools",
        "/registry/skills/pdf-tools/versions/1.1.0/files/SKILL.md",
    ]

    for path in paths:
        accepted = await site.get(f"{path}?pad={padding}")
        refused = await site.get(f"{path}?pad={padding}x")
        assert accepted.status_code == 200, path
        assert refused.status_code == 400, path
        assert refused.headers["content-security-policy"] == CSP
        assert f"at most {public.MAX_QUERY_STRING} characters" in refused.text


async def test_the_largest_accepted_filter_state_still_gives_a_bounded_page(site, library):
    pairs = [("q", "q" * 512), ("category", "c" * 256), ("kind", "skill"), ("sort", "name")]
    pairs += [("tag", chr(97 + index) * 256) for index in range(public.MAX_TAG_FILTERS)]
    query = urlencode(pairs)
    assert len(query) < public.MAX_QUERY_STRING

    started = time.perf_counter()
    response = await site.get(f"/registry?{query}")
    elapsed = time.perf_counter() - started

    assert response.status_code == 200
    assert len(response.content) < 1024 * 1024
    assert elapsed < 1.0


async def test_the_registry_page_of_a_private_registry_answers_401_and_lists_nothing(
    site, db_session, blob_store, actors, monkeypatch
):
    await publish(db_session, blob_store, actors.owner, "hidden-treasure", tags=["secret-tag"])
    monkeypatch.setattr(settings.registry, "public_read", False)

    page = await visit(site, "/registry", 401)

    assert page.response.headers["www-authenticate"] == "Bearer"
    assert page.find("h1").words() == "This registry is private"
    assert "Authorization: Bearer" in page.find("p", cls="lead").words()
    text = page.response.text
    for secret in ("hidden-treasure", "secret-tag", "A sample package"):
        assert secret not in text
    assert not page.find_all("article", cls="card")
    assert not page.find_all("form")
    for query in ("?q=hidden", "?kind=skill", "?tag=secret-tag", "?page=2"):
        again = await visit(site, f"/registry{query}", 401)
        assert "hidden-treasure" not in again.response.text


RICH_BODY = """# PDF tools

Intro with **bold**, *emphasis*, ~~old~~ and `code`.

## When to use

Read the [spec notes](references/guide.md "The guide"), run the [script](./scripts/run.py#L1)
or see [a missing page](references/nope.md). Jump to [usage](#usage) or the [absent](#nowhere).

## Usage

| Option | Meaning |
|:--|--:|
| `--ocr` | Scan pages |

```bash
python scripts/run.py input.pdf
```

1. first
2. second

> quoted

![diagram](docs/diagram.png)

[site](https://example.com/a?b=c), <https://auto.example.org> and [mail](mailto:a@example.org).
"""

RICH_FILES = {
    "scripts/run.py": "print('run')\n",
    "scripts/lib/util.py": "VALUE = 1\n",
    "references/guide.md": "# Guide\n",
    "assets/logo.png": b"\x89PNG\r\n\x1a\n" + bytes(range(64)),
    "docs/hello world #1?.md": "# Hello\n",
    "docs/markup.html": "<b>\"hi\" & 'bye'</b>\n<script>alert(1)</script>\n",
    "docs/bidi.txt": "safe " + chr(0x202E) + "evil\n",
    "docs/long.txt": "line\n" * 100,
    "unicode-" + chr(0xFC) + "/" + chr(0xDF) + ".txt": "unicode\n",
    "100%.txt": "percent\n",
    "a+b&c.txt": "plus\n",
}


@pytest_asyncio.fixture
async def showcase(db_session, blob_store, actors) -> Principal:
    owner = actors.owner
    base = {
        "display_name": "PDF Tools",
        "description": "Extract text and tables from PDF files.",
        "tags": ["pdf", "docs"],
        "category": "documents",
    }
    await publish(
        db_session,
        blob_store,
        owner,
        "pdf-tools",
        "0.9.0",
        body="# Old release\n\nGone.\n",
        files={"scripts/run.py": "print('old')\n"},
        **base,
    )
    await publish(
        db_session,
        blob_store,
        owner,
        "pdf-tools",
        "1.0.0",
        body="# Version one\n\nFirst stable.\n",
        files={"scripts/run.py": "print('one')\n"},
        **base,
    )
    await publish(
        db_session,
        blob_store,
        owner,
        "pdf-tools",
        "1.1.0",
        body=RICH_BODY,
        files=RICH_FILES,
        extra_frontmatter={"license": "MIT", "author": "Ada", "stars": 5, "nested": {"a": [1, 2]}},
        **base,
    )
    await publish(
        db_session,
        blob_store,
        owner,
        "pdf-tools",
        "2.0.0",
        live=False,
        body="# Draft two\n\nNot yet.\n",
        **base,
    )
    await VersionService(db_session).deprecate(owner, "pdf-tools", "0.9.0")
    return owner


async def test_the_detail_page_shows_the_header_of_the_entry(site, showcase):
    page = await visit(site, "/registry/skills/pdf-tools")

    assert page.find("title").words() == f"PDF Tools 1.1.0 {MIDDLE_DOT} Alexandria"
    assert page.find("h1").words() == "PDF Tools"
    assert page.find("code", cls="detail__name").words() == "pdf-tools"
    assert page.find("span", cls="badge--skill").words() == "Skill"
    assert page.find("span", cls="version--large").words() == "v1.1.0"
    assert page.find("p", cls="detail__description").words() == (
        "Extract text and tables from PDF files."
    )
    chips = {a.words(): a.attrs["href"] for a in page.find("ul", cls="chips").find_all("a")}
    assert chips == {
        "documents": "/registry?category=documents",
        "docs": "/registry?tag=docs",
        "pdf": "/registry?tag=pdf",
    }
    assert page.find("meta", name="description").attrs["content"] == (
        "Extract text and tables from PDF files."
    )
    assert page.find("link", rel="canonical").attrs["href"] == (
        f"{PUBLIC_URL}/registry/skills/pdf-tools"
    )
    crumbs = page.find("nav", **{"aria-label": "Breadcrumb"})
    assert [a.attrs["href"] for a in crumbs.find_all("a")] == ["/registry"]
    assert not page.find_all("p", cls="notice")


async def test_the_facts_of_the_detail_page_describe_the_version(
    site, showcase, db_session, blob_store
):
    detail = await VersionService(db_session).get(None, "pdf-tools", "1.1.0")
    listing = await PackageService(db_session, blob_store).manifest(None, "pdf-tools", "1.1.0")

    page = await visit(site, "/registry/skills/pdf-tools")

    found = facts(page)
    assert found["Versions"] == "3"
    assert found["Published"] == detail.published_at.date().isoformat()
    assert found["Package"] == public.human_size(detail.size_bytes)
    assert found["Files"] == str(len(listing.files)) == str(detail.file_count)
    assert found["SHA-256"] == f"{detail.sha256[:12]}{ELLIPSIS}"
    assert page.find("code", title=detail.sha256)


async def test_the_use_block_shows_the_mcp_call_curl_and_alx(site, showcase, db_session):
    detail = await VersionService(db_session).get(None, "pdf-tools", "1.1.0")

    page = await visit(site, "/registry/skills/pdf-tools")

    assert page.by_id("use-mcp").text() == (
        'get_entrypoint {"name": "pdf-tools", "version": "1.1.0"}'
    )
    url = f"{PUBLIC_URL}/api/v1/skills/pdf-tools/versions/1.1.0/download"
    assert page.by_id("use-curl").text() == (
        f"curl -fL -o pdf-tools-1.1.0.zip {url}\n"
        f"echo '{detail.sha256}  pdf-tools-1.1.0.zip' | sha256sum -c -"
    )
    assert page.by_id("use-alx").text() == (
        f"export ALX_URL={PUBLIC_URL}\nalx pull pdf-tools@1.1.0"
    )
    panel = page.by_id("use-title").parent
    download = panel.find("p", cls="panel__foot").find("a")
    assert download.attrs["href"] == "/api/v1/skills/pdf-tools/versions/1.1.0/download"
    assert download.words() == "Download pdf-tools-1.1.0.zip"
    for button in panel.find_all("button"):
        assert "hidden" in button.attrs and button.attrs["type"] == "button"
    assert [b.attrs["data-copy"] for b in panel.find_all("button")] == [
        "use-mcp",
        "use-curl",
        "use-alx",
    ]


async def test_the_entry_point_is_rendered_as_markdown_without_its_frontmatter(site, showcase):
    page = await visit(site, "/registry/skills/pdf-tools")

    article = page.find("article", cls="detail__entry")
    assert article.by_id("entry-title").words() == "SKILL.md"
    source = article.find("a", cls="panel__action")
    assert source.attrs["href"] == "/registry/skills/pdf-tools/versions/1.1.0/files/SKILL.md"
    prose = article.find("div", cls="prose")
    assert [(h.tag, h.words()) for h in prose.find_all() if re.fullmatch(r"h\d", h.tag)] == [
        ("h2", "PDF tools"),
        ("h3", "When to use"),
        ("h3", "Usage"),
    ]
    assert [h.attrs["id"] for h in prose.find_all("h3")] == ["md-when-to-use", "md-usage"]
    text = prose.text()
    for leaked in ("description:", "name: pdf-tools", "version: 1.1.0", "license: MIT", "---"):
        assert leaked not in text
    assert prose.find("strong").words() == "bold"
    assert prose.find("em").words() == "emphasis"
    assert prose.find("s").words() == "old"
    assert prose.find("blockquote").words() == "quoted"
    assert [li.words() for li in prose.find("ol").find_all("li")] == ["first", "second"]


async def test_markdown_links_are_resolved_inside_the_package_and_outside_are_marked(
    site, showcase
):
    page = await visit(site, "/registry/skills/pdf-tools")

    prose = page.find("div", cls="prose")
    links = {a.words(): a for a in prose.find_all("a")}
    base = "/registry/skills/pdf-tools/versions/1.1.0/files"
    assert links["spec notes"].attrs["href"] == f"{base}/references/guide.md"
    assert links["spec notes"].attrs["title"] == "The guide"
    assert links["script"].attrs["href"] == f"{base}/scripts/run.py"
    assert links["usage"].attrs["href"] == "#md-usage"
    assert "rel" not in links["spec notes"].attrs
    for label, target in (
        ("site", "https://example.com/a?b=c"),
        ("https://auto.example.org", "https://auto.example.org"),
        ("mail", "mailto:a@example.org"),
    ):
        assert links[label].attrs["href"] == target
        assert links[label].attrs["rel"] == "nofollow ugc noreferrer"
    assert "a missing page" not in links and "absent" not in links
    assert "a missing page" in prose.text() and "absent" in prose.text()
    assert prose.find("span", cls="md-image").words() == "Image: diagram"
    assert not prose.find_all("img")


async def test_markdown_tables_and_code_blocks_are_focusable_and_use_classes_not_styles(
    site, showcase
):
    page = await visit(site, "/registry/skills/pdf-tools")

    prose = page.find("div", cls="prose")
    wrapper = prose.find("div", cls="md-table")
    assert wrapper.attrs["tabindex"] == "0" and wrapper.attrs["role"] == "region"
    headers = wrapper.find_all("th")
    assert [(h.words(), h.classes) for h in headers] == [
        ("Option", ["align-left"]),
        ("Meaning", ["align-right"]),
    ]
    block = prose.find("pre")
    assert block.attrs["tabindex"] == "0"
    assert block.find("code").classes == ["language-bash"]
    assert block.text() == "python scripts/run.py input.pdf\n"
    assert not [n for n in page.walk() if "style" in n.attrs]


async def test_the_versions_table_lists_visible_versions_newest_first(site, showcase, db_session):
    summaries = {v.version: v for v in await VersionService(db_session).list(None, "pdf-tools")}

    page = await visit(site, "/registry/skills/pdf-tools")

    table = page.find("table", cls="versions")
    rows = table.find("tbody").find_all("tr")
    assert [r.find("th").find("a").words() for r in rows] == ["1.1.0", "1.0.0", "0.9.0"]
    assert [r.find("span", cls="badge").words() for r in rows] == [
        "Published",
        "Published",
        "Deprecated",
    ]
    assert [bool(r.find_all("span", cls="tag-latest")) for r in rows] == [True, False, False]
    assert [("is-selected" in r.classes) for r in rows] == [True, False, False]
    for row in rows:
        version = row.find("th").find("a").words()
        summary = summaries[version]
        cells = row.find_all("td")
        assert cells[1].words() == summary.published_at.date().isoformat()
        assert cells[2].words() == public.human_size(summary.size_bytes)
        assert cells[3].words() == summary.sha256[:12]
        assert cells[3].find("code").attrs["title"] == summary.sha256
        link = cells[4].find("a")
        assert link.attrs["href"] == f"/api/v1/skills/pdf-tools/versions/{version}/download"
        assert link.words() == f"Download version {version}"
        assert row.find("th").find("a").attrs["href"] == (
            f"/registry/skills/pdf-tools?version={version}"
        )
    assert "2.0.0" not in table.text()
    assert page.find("section", id="versions")


async def test_the_file_tree_lists_the_manifest_with_sizes_and_links(
    site, showcase, db_session, blob_store
):
    listing = await PackageService(db_session, blob_store).manifest(None, "pdf-tools", "1.1.0")
    sizes = {entry.path: public.human_size(entry.size) for entry in listing.files}

    page = await visit(site, "/registry/skills/pdf-tools")

    tree = page.find("section", cls="detail__files").find("ul", cls="tree")
    top = [li for li in tree.children if isinstance(li, Node)]
    assert [li.find("span", cls="tree__name").words() for li in top] == [
        "assets",
        "docs",
        "references",
        "scripts",
        "unicode-" + chr(0xFC),
        "100%.txt",
        "a+b&c.txt",
        "SKILL.md",
    ]
    files = {}
    for item in tree.find_all("li", cls="tree__file"):
        link = item.find("a")
        files[link.attrs["href"]] = (item.find("span", cls="tree__meta").words(), link)
    assert len(files) == len(listing.files)
    for entry in listing.files:
        url = public.file_url("pdf-tools", "1.1.0", entry.path)
        assert files[url][0] == sizes[entry.path]
    entry_item = tree.find("span", cls="tree__tag")
    assert entry_item.words() == "entry point"
    assert entry_item.parent.find("a").attrs["href"].endswith("/files/SKILL.md")
    scripts = next(li for li in top if li.find("span", cls="tree__name").words() == "scripts")
    assert scripts.find("span", cls="tree__meta").words() == "2 files"
    assert "open" in scripts.find("details").attrs
    assert [a.words() for a in scripts.find("ul").find_all("a")] == ["util.py", "run.py"]
    assert page.find("h2", id="files-title").words() == f"Files {len(listing.files)}"


async def test_the_metadata_of_the_package_is_listed_as_text(site, showcase):
    page = await visit(site, "/registry/skills/pdf-tools")

    section = page.find("section", cls="detail__meta")
    items = {d.find("dt").words(): d.find("dd").words() for d in section.find_all("div")}
    assert items == {
        "author": "Ada",
        "license": "MIT",
        "nested": '{"a": [1, 2]}',
        "stars": "5",
    }


async def test_the_detail_page_of_an_entry_without_metadata_has_no_metadata_section(
    site, db_session, blob_store, actors
):
    await publish(db_session, blob_store, actors.owner, "plain")

    page = await visit(site, "/registry/skills/plain")

    assert not page.find_all("section", cls="detail__meta")


async def test_an_older_version_can_be_chosen_with_the_version_parameter(site, showcase):
    page = await visit(site, "/registry/skills/pdf-tools?version=1.0.0")

    assert page.find("title").words() == f"PDF Tools 1.0.0 {MIDDLE_DOT} Alexandria"
    assert page.find("span", cls="version--large").words() == "v1.0.0"
    notice = page.find("p", cls="notice")
    assert notice.words() == (
        "You are viewing version 1.0.0. The latest version is 1.1.0. View the latest version"
    )
    assert notice.find("a").attrs["href"] == "/registry/skills/pdf-tools"
    assert page.find("div", cls="prose").find("h2").words() == "Version one"
    assert page.by_id("use-mcp").text() == (
        'get_entrypoint {"name": "pdf-tools", "version": "1.0.0"}'
    )
    assert page.by_id("use-alx").text().endswith("alx pull pdf-tools@1.0.0")
    names = [a.words() for a in page.find("ul", cls="tree").find_all("a")]
    assert names == ["run.py", "SKILL.md"]
    selected = page.find("tr", cls="is-selected")
    assert selected.find("th").words().startswith("1.0.0")
    assert not page.find_all("section", cls="detail__meta")


async def test_a_deprecated_version_is_marked_but_still_readable(site, showcase):
    page = await visit(site, "/registry/skills/pdf-tools?version=0.9.0")

    assert page.find("span", cls="badge--deprecated")
    notice = page.find("p", cls="notice--warn")
    assert "Version 0.9.0 is deprecated since" in notice.words()
    assert notice.find("a").attrs["href"] == "/registry/skills/pdf-tools"
    assert page.find("div", cls="prose").find("h2").words() == "Old release"
    assert "Gone." in page.find("div", cls="prose").words()


@pytest.mark.parametrize("version", ["latest", ""])
async def test_latest_and_an_empty_version_choose_the_latest_published_one(site, showcase, version):
    page = await visit(site, f"/registry/skills/pdf-tools?version={version}")

    assert page.find("span", cls="version--large").words() == "v1.1.0"
    assert not page.find_all("p", cls="notice")


@pytest.mark.parametrize(
    "version",
    [
        "9.9.9",
        "2.0.0",
        "1.1",
        "v1.1.0",
        "1.1.0 ",
        "%00",
        "x" * 100,
        "<script>alert(1)</script>",
        "../../etc",
        "1.1.0%00",
        "LATEST",
        "1.1.0-beta",
    ],
)
async def test_a_version_that_is_missing_or_not_public_is_a_404(site, showcase, version):
    page = await visit(site, f"/registry/skills/pdf-tools?version={version}", 404)

    assert page.find("h1").words() == "Not found"
    assert "Version not found" in page.find("p", cls="lead").words()


async def test_a_draft_version_gives_the_same_404_as_one_that_never_existed(site, showcase):
    draft = await visit(site, "/registry/skills/pdf-tools?version=2.0.0", 404)
    never = await visit(site, "/registry/skills/pdf-tools?version=3.0.0", 404)

    assert draft.response.text.replace("2.0.0", "X") == never.response.text.replace("3.0.0", "X")
    assert "Draft two" not in draft.response.text


async def test_an_entry_can_be_opened_by_uuid_and_still_links_by_name(site, showcase, db_session):
    entry = await SkillService(db_session).get(None, "pdf-tools")

    page = await visit(site, f"/registry/skills/{entry.id}?version=1.0.0")

    assert page.find("h1").words() == "PDF Tools"
    assert page.find("link", rel="canonical").attrs["href"] == (
        f"{PUBLIC_URL}/registry/skills/pdf-tools"
    )
    assert entry.id not in page.response.text
    assert page.find("tr", cls="is-selected").find("th").words().startswith("1.0.0")


async def test_a_grounding_entry_shows_its_index_as_entry_point(
    site, db_session, blob_store, actors
):
    await publish(
        db_session,
        blob_store,
        actors.owner,
        "style-guide",
        kind="grounding",
        body="# Style\n\nReference text.\n",
        description="Reference.",
    )

    page = await visit(site, "/registry/skills/style-guide")

    assert page.find("span", cls="badge--grounding").words() == "Grounding"
    assert page.by_id("entry-title").words() == "INDEX.md"
    assert page.find("div", cls="prose").find("h2").words() == "Style"
    assert page.by_id("use-mcp").text().startswith("get_entrypoint ")


@pytest.mark.parametrize("state", ["draft", "hidden"])
async def test_entries_that_are_not_public_answer_exactly_like_missing_ones(
    site, db_session, blob_store, actors, state
):
    owner = actors.owner
    await publish(
        db_session,
        blob_store,
        owner,
        "secret-entry",
        live=state != "draft",
        description="Top secret description",
    )
    if state == "hidden":
        await SkillService(db_session).hide(owner, "secret-entry")
    entry = await SkillService(db_session).get(owner, "secret-entry")

    by_name_page = await visit(site, "/registry/skills/secret-entry", 404)
    missing = await visit(site, "/registry/skills/nothere-entry", 404)
    by_uuid = await visit(site, f"/registry/skills/{entry.id}", 404)
    missing_uuid = await visit(site, "/registry/skills/00000000-0000-4000-8000-000000000000", 404)

    assert by_name_page.response.text.replace("secret-entry", "X") == (
        missing.response.text.replace("nothere-entry", "X")
    )
    assert by_uuid.response.text.replace(entry.id, "X") == missing_uuid.response.text.replace(
        "00000000-0000-4000-8000-000000000000", "X"
    )
    for page in (by_name_page, by_uuid):
        assert "Top secret description" not in page.response.text
    for suffix in ("?version=1.0.0", "/versions/1.0.0/files/SKILL.md"):
        url = f"/registry/skills/secret-entry{suffix}"
        again = await visit(site, url, 404)
        assert "Top secret description" not in again.response.text
    listing = await visit(site, "/registry")
    assert "secret-entry" not in listing.response.text


async def test_a_long_entry_point_is_cut_by_the_service_and_the_page_says_so(
    site, db_session, blob_store, actors, monkeypatch
):
    body = "".join(f"Paragraph number {i} of the long document.\n\n" for i in range(200))
    await publish(db_session, blob_store, actors.owner, "long-doc", body=body)
    monkeypatch.setattr(settings.registry.limits, "max_inline_read_bytes", 600)

    page = await visit(site, "/registry/skills/long-doc")

    notice = page.find("p", cls="notice")
    assert "This entry point is long" in notice.words()
    assert (
        notice.find("a").attrs["href"] == "/registry/skills/long-doc/versions/1.0.0/files/SKILL.md"
    )
    paragraphs = page.find("div", cls="prose").find_all("p")
    assert 0 < len(paragraphs) < 20


async def test_the_page_renders_only_a_bounded_prefix_of_a_huge_markdown_file(
    site, db_session, blob_store, actors, monkeypatch
):
    body = "".join(f"Paragraph number {i} of the document.\n\n" for i in range(300))
    await publish(db_session, blob_store, actors.owner, "huge-doc", body=body)
    monkeypatch.setattr(public, "MARKDOWN_LIMIT", 500)

    page = await visit(site, "/registry/skills/huge-doc")

    assert "This entry point is long" in page.find("p", cls="notice").words()
    paragraphs = [p.words() for p in page.find("div", cls="prose").find_all("p")]
    assert 3 < len(paragraphs) < 30
    assert all(p.endswith("of the document.") for p in paragraphs)


async def test_a_missing_package_blob_is_a_generic_500_without_details(
    site, db_session, blob_store, actors
):
    await publish(db_session, blob_store, actors.owner, "broken-pkg")
    sha = (await VersionService(db_session).get(None, "broken-pkg")).sha256
    await blob_store.delete(sha)

    page = await visit(site, "/registry/skills/broken-pkg", 500)

    assert page.find("h1").words() == "Something went wrong"
    assert page.find("p", cls="lead").words() == (
        "The server could not complete the request. Try again later."
    )
    for leak in (sha, str(blob_store.root), "StorageError", "Traceback"):
        assert leak not in page.response.text
    assert page.find("meta", name="robots").attrs["content"] == "noindex"


async def test_the_detail_page_of_a_private_registry_answers_401_for_everything(
    site, showcase, monkeypatch
):
    monkeypatch.setattr(settings.registry, "public_read", False)

    for url in (
        "/registry/skills/pdf-tools",
        "/registry/skills/pdf-tools?version=1.0.0",
        "/registry/skills/unknown-entry",
        "/registry/skills/pdf-tools/versions/1.1.0/files/SKILL.md",
        "/registry/skills/pdf-tools/versions/latest/files/scripts/run.py",
    ):
        page = await visit(site, url, 401)
        assert page.response.headers["www-authenticate"] == "Bearer"
        assert page.find("h1").words() == "This registry is private"
        for secret in ("PDF Tools", "pdf-tools", "Extract text", "print("):
            assert secret not in page.response.text


async def test_the_file_page_shows_a_text_file_exactly_as_text(site, showcase):
    page = await visit(site, "/registry/skills/pdf-tools/versions/1.1.0/files/scripts/run.py")

    assert page.find("title").words() == (
        f"scripts/run.py {MIDDLE_DOT} pdf-tools 1.1.0 {MIDDLE_DOT} Alexandria"
    )
    assert page.find("h1").find("code").words() == "scripts/run.py"
    block = page.by_id("file-content")
    assert block.tag == "pre" and block.attrs["tabindex"] == "0"
    assert block.text() == "print('run')\n"
    found = facts(page)
    assert found["Size"] == "13 B"
    assert found["Type"] == "text/x-python"
    assert found["Version"] == "1.1.0"
    assert re.fullmatch(r"[0-9a-f]{64}", found["SHA-256"])
    raw = page.find("a", href="/api/v1/skills/pdf-tools/versions/1.1.0/raw/scripts/run.py")
    assert raw.words() == "Raw file"
    assert page.find("meta", name="robots").attrs["content"] == "noindex"
    copy = page.find("button", **{"data-copy": "file-content"})
    assert "hidden" in copy.attrs
    crumbs = page.find("nav", **{"aria-label": "Breadcrumb"})
    assert [a.attrs["href"] for a in crumbs.find_all("a")] == [
        "/registry",
        "/registry/skills/pdf-tools?version=1.1.0",
    ]
    assert crumbs.find("span", **{"aria-current": "page"}).words() == "scripts/run.py"


async def test_the_file_page_hash_and_size_match_the_manifest(
    site, showcase, db_session, blob_store
):
    listing = await PackageService(db_session, blob_store).manifest(None, "pdf-tools", "1.1.0")

    for entry in listing.files:
        page = await visit(site, public.file_url("pdf-tools", "1.1.0", entry.path))
        found = facts(page)
        assert found["SHA-256"] == entry.sha256
        assert found["Size"] == public.human_size(entry.size)
        assert found["Type"] == entry.media_type


async def test_the_file_page_marks_the_current_file_in_the_tree(site, showcase):
    page = await visit(site, "/registry/skills/pdf-tools/versions/1.1.0/files/scripts/lib/util.py")

    current = page.find_all("li", cls="is-current")
    assert len(current) == 1
    assert current[0].find("a").attrs["aria-current"] == "page"
    assert current[0].find("a").words() == "util.py"
    directories = {
        d.find("summary").find("span", cls="tree__name").words(): d
        for d in page.find("ul", cls="tree").find_all("details")
    }
    assert "open" in directories["scripts"].attrs and "open" in directories["lib"].attrs


async def test_latest_works_as_the_version_of_a_file_url_and_resolves_to_the_exact_one(
    site, showcase
):
    page = await visit(site, "/registry/skills/pdf-tools/versions/latest/files/scripts/run.py")

    assert facts(page)["Version"] == "1.1.0"
    hrefs = [a.attrs["href"] for a in page.find("ul", cls="tree").find_all("a")]
    assert hrefs and all("/versions/1.1.0/files/" in href for href in hrefs)
    assert all("latest" not in href for href in hrefs)


async def test_an_older_version_serves_its_own_file_content(site, showcase):
    page = await visit(site, "/registry/skills/pdf-tools/versions/1.0.0/files/scripts/run.py")

    assert page.by_id("file-content").text() == "print('one')\n"
    deprecated = await visit(site, "/registry/skills/pdf-tools/versions/0.9.0/files/scripts/run.py")
    assert deprecated.by_id("file-content").text() == "print('old')\n"


async def test_the_file_page_of_a_binary_shows_a_card_and_a_download_link(
    site, showcase, db_session, blob_store
):
    listing = await PackageService(db_session, blob_store).manifest(None, "pdf-tools", "1.1.0")
    entry = next(item for item in listing.files if item.path == "assets/logo.png")

    page = await visit(site, "/registry/skills/pdf-tools/versions/1.1.0/files/assets/logo.png")

    card = page.find("div", cls="binary")
    assert card.find("h2").words() == "Binary file"
    link = card.find("a")
    assert link.attrs["href"] == "/api/v1/skills/pdf-tools/versions/1.1.0/raw/assets/logo.png"
    assert link.words() == f"Download {public.human_size(entry.size)}"
    assert not page.find_all("pre")
    assert facts(page)["SHA-256"] == entry.sha256
    assert facts(page)["Type"] == entry.media_type
    assert not page.find_all("button", **{"data-copy": "file-content"})


async def test_file_content_with_markup_is_shown_as_text_and_never_parsed(site, showcase):
    page = await visit(site, "/registry/skills/pdf-tools/versions/1.1.0/files/docs/markup.html")

    assert page.by_id("file-content").text() == RICH_FILES["docs/markup.html"]
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.response.text
    assert "<script>alert" not in page.response.text
    assert not page.by_id("file-content").find_all("script")
    assert not page.by_id("file-content").find_all("b")


async def test_bidirectional_control_characters_in_a_file_are_made_visible(site, showcase):
    page = await visit(site, "/registry/skills/pdf-tools/versions/1.1.0/files/docs/bidi.txt")

    assert page.by_id("file-content").text() == "safe <U+202E>evil\n"
    assert chr(0x202E) not in page.response.text


async def test_a_file_larger_than_the_inline_limit_is_cut_and_explained(
    site, showcase, monkeypatch
):
    monkeypatch.setattr(settings.registry.limits, "max_inline_read_bytes", 50)

    page = await visit(site, "/registry/skills/pdf-tools/versions/1.1.0/files/docs/long.txt")

    assert "only the first part is shown" in page.find("p", cls="notice").words()
    assert len(page.by_id("file-content").text()) == 50
    assert facts(page)["Size"] == "500 B"
    short = await visit(site, "/registry/skills/pdf-tools/versions/1.1.0/files/scripts/run.py")
    assert not short.find_all("p", cls="notice")


async def test_every_link_of_the_tree_opens_the_file_it_names(
    site, showcase, db_session, blob_store
):
    service = PackageService(db_session, blob_store)
    listing = await service.manifest(None, "pdf-tools", "1.1.0")
    page = await visit(site, "/registry/skills/pdf-tools")

    links = [a.attrs["href"] for a in page.find("ul", cls="tree").find_all("a")]
    assert len(links) == len(listing.files) == len(set(links))
    for href in links:
        opened = await visit(site, href)
        path = opened.find("h1").find("code").words()
        entry = next(item for item in listing.files if item.path == path)
        assert href == public.file_url("pdf-tools", "1.1.0", path)
        content = await service.read_file(None, "pdf-tools", "1.1.0", path)
        if entry.text:
            assert opened.by_id("file-content").text() == public.reveal_bidi(content.content)
        else:
            assert opened.find("div", cls="binary")
        raw = opened.find("a", cls="button").attrs["href"]
        assert raw == public.raw_url("pdf-tools", "1.1.0", path)
        assert raw.startswith("/api/v1/skills/pdf-tools/versions/1.1.0/raw/")


@pytest.mark.parametrize(
    "path",
    [
        "%2e%2e/%2e%2e/etc/passwd",
        "..%2fSKILL.md",
        "scripts/%2e%2e/%2e%2e/SKILL.md",
        "scripts/%2e%2e/SKILL.md",
        "scripts%5crun.py",
        "scripts%5c..%5cSKILL.md",
        "C:/Windows/win.ini",
        "/etc/passwd",
        "%2fetc%2fpasswd",
        "scripts/run.py%00.txt",
        "%00",
        "scripts//run.py",
        "scripts/%2e/run.py",
        "scripts/",
        "SKILL.md/",
        "skill.md",
        "scripts/run.py%20",
        "scripts/Run.py",
        "a" * 300,
        "scripts/lib",
        "nothing/here.txt",
        "%e2%80%ae",
    ],
)
async def test_hostile_or_unknown_file_paths_are_404_and_never_read_the_blob(
    site, showcase, spy, path
):
    spy.reads.clear()

    page = await visit(site, f"/registry/skills/pdf-tools/versions/1.1.0/files/{path}", 404)

    assert page.find("h1").words() == "Not found"
    assert spy.reads == []
    assert "root:" not in page.response.text


@pytest.mark.parametrize(
    "url",
    [
        "/registry/skills/pdf-tools/versions/2.0.0/files/SKILL.md",
        "/registry/skills/pdf-tools/versions/9.9.9/files/SKILL.md",
        "/registry/skills/pdf-tools/versions/%2e%2e/files/SKILL.md",
        "/registry/skills/pdf-tools/versions/1.1.0%00/files/SKILL.md",
        "/registry/skills/ghost/versions/1.0.0/files/SKILL.md",
    ],
)
async def test_files_of_missing_or_draft_versions_and_entries_are_404(site, showcase, spy, url):
    spy.reads.clear()

    await visit(site, url, 404)

    assert spy.reads == []


async def test_the_file_page_accepts_the_uuid_but_links_by_name(site, showcase, db_session):
    entry = await SkillService(db_session).get(None, "pdf-tools")

    page = await visit(site, f"/registry/skills/{entry.id}/versions/1.1.0/files/SKILL.md")

    assert entry.id not in page.response.text
    assert facts(page)["Version"] == "1.1.0"
    assert page.find("a", href="/registry/skills/pdf-tools?version=1.1.0")


async def test_the_404_page_explains_where_to_go_next(site):
    page = await visit(site, "/registry/skills/ghost", 404)

    assert page.find("title").words() == f"404 Not found {MIDDLE_DOT} Alexandria"
    assert page.find("h1").words() == "Not found"
    assert page.find("p", cls="lead").words() == "Skill not found: 'ghost'"
    assert page.find("p", cls="errorpage__code").words() == "404"
    assert {a.attrs["href"] for a in page.find("p", cls="actions").find_all("a")} == {
        "/registry",
        "/",
    }
    assert page.find("meta", name="robots").attrs["content"] == "noindex"
    assert not page.find_all("link", rel="canonical")


@pytest.mark.parametrize(
    "url",
    [
        "/registry/ghost",
        "/registry/skills",
        "/registry/skills/x/versions",
        "/registry/skills/x/versions/1.0.0",
        "/registry/skills/x/",
        "/registry/skills/x/versions/1.0.0/files",
    ],
)
async def test_unknown_pages_under_the_registry_get_the_site_404_page(site, url):
    page = await visit(site, url, 404)

    assert page.find("h1").words() == "Not found"
    assert page.find("p", cls="lead").words() == "There is no such page in the registry"


@pytest.mark.parametrize(
    "reference",
    ["<img src=x onerror=alert(1)>", '"><svg onload=alert(1)>', "<script>alert(1)<script>"],
)
async def test_hostile_references_are_echoed_as_escaped_text_only(site, reference):
    page = await visit(site, f"/registry/skills/{reference}", 404)

    assert page.find("p", cls="lead").text() == f"Skill not found: '{reference}'"
    assert "<img src=x" not in page.response.text and "<svg onload" not in page.response.text
    assert "<script>alert" not in page.response.text


async def test_the_static_assets_exist_and_the_pages_reference_them_with_a_version(site):
    page = await visit(site, "/")

    stylesheet = page.find("link", rel="stylesheet").attrs["href"]
    script = page.find("script").attrs["src"]
    icon = page.find("link", rel="icon").attrs["href"]
    assert stylesheet.startswith(f"{public.STATIC_URL}/style.css?v=")
    assert script.startswith(f"{public.STATIC_URL}/site.js?v=")
    assert icon.startswith(f"{public.STATIC_URL}/favicon.svg?v=")
    assert "defer" in page.find("script").attrs
    css = await site.get(stylesheet)
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    js = await site.get(script)
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    svg = await site.get(icon)
    assert svg.status_code == 200 and svg.headers["content-type"].startswith("image/svg+xml")


async def test_the_nav_marks_the_registry_as_the_current_page_there(site):
    registry = await visit(site, "/registry")
    landing = await visit(site, "/")

    current = registry.find("nav", **{"aria-label": "Main"}).find("a", **{"aria-current": "page"})
    assert current.attrs["href"] == "/registry"
    assert not landing.find("nav", **{"aria-label": "Main"}).find_all(
        "a", **{"aria-current": "page"}
    )
    nav = {a.attrs["href"] for a in landing.find("nav", **{"aria-label": "Main"}).find_all("a")}
    assert nav == {"/registry", "/docs", "/admin/"}


HOSTILE_BODY = """# <script>alert('h1')</script>

<img src=x onerror=alert(1)>

<div onclick="alert(2)">raw html</div>

<iframe src="https://evil.example"></iframe>

[click](javascript:alert(3))

[entity](java&#x73;cript:alert(4))

[data](data:text/html;base64,PHNjcmlwdD5hbGVydCg1KTwvc2NyaXB0Pg==)

<javascript:alert(6)>

![pixel](https://tracker.example/pixel.png)

```html
<script>alert(7)</script>
<style>body{display:none}</style>
```

[ref link][bad]

[bad]: javascript:alert(8)
"""

HOSTILE_NAME = '<script>alert("name")</script>'
HOSTILE_DESCRIPTION = "<img src=x onerror=alert(1)> \"quoted\" 'single' & <b>bold</b>"
HOSTILE_FILES = {
    "<script>alert(10)<script>.txt": "<script>alert(11)</script>\n",
    '"><img src=x onerror=alert(12)>.md': "# x\n",
    "<dir onclick=alert(13)>/a'b\"c&d.txt": "x\n",
}


@pytest_asyncio.fixture
async def hostile(db_session, blob_store, actors) -> None:
    await publish(
        db_session,
        blob_store,
        actors.owner,
        "xss-lab",
        display_name=HOSTILE_NAME,
        description=HOSTILE_DESCRIPTION,
        tags=["xss"],
        category="security",
        body=HOSTILE_BODY,
        files=HOSTILE_FILES,
        extra_frontmatter={"homepage": "<script>alert(9)</script>", "<b>key</b>": "<i>value</i>"},
    )


async def test_a_hostile_package_is_displayed_as_text_in_the_detail_page(site, hostile):
    page = await visit(site, "/registry/skills/xss-lab")

    assert page.find("h1").text() == HOSTILE_NAME
    assert page.find("title").text().startswith(HOSTILE_NAME)
    assert page.find("p", cls="detail__description").text() == HOSTILE_DESCRIPTION
    assert "&lt;script&gt;alert(&#34;name&#34;)&lt;/script&gt;" in page.response.text
    prose = page.find("div", cls="prose")
    text = prose.text()
    for shown in (
        "<script>alert('h1')</script>",
        "<img src=x onerror=alert(1)>",
        '<div onclick="alert(2)">raw html</div>',
        '<iframe src="https://evil.example"></iframe>',
        "[click](javascript:alert(3))",
        "[entity](javascript:alert(4))",
        "<javascript:alert(6)>",
        "<script>alert(7)</script>",
        "<style>body{display:none}</style>",
        "[ref link][bad]",
        "[bad]: javascript:alert(8)",
    ):
        assert shown in text, shown
    assert prose.find("span", cls="md-image").words() == "Image: pixel"
    assert not prose.find_all("a")
    metadata = page.find("section", cls="detail__meta")
    shown = {d.find("dt").text(): d.find("dd").text() for d in metadata.find_all("div")}
    assert shown == {"<b>key</b>": "<i>value</i>", "homepage": "<script>alert(9)</script>"}
    assert [
        node.tag for node in page.walk() if node.tag in {"b", "i", "iframe", "img", "style"}
    ] == []
    assert len(page.find_all("script")) == 1


async def test_a_hostile_package_is_displayed_as_text_in_listings_and_the_landing_page(
    site, hostile
):
    for url in (
        "/",
        "/registry",
        "/registry?q=xss",
        "/registry?tag=xss",
        "/registry?category=security",
    ):
        page = await visit(site, url)

        card = by_name(page)["xss-lab"]
        assert card.find("a", cls="card__link").text() == HOSTILE_NAME
        assert card.find("p", cls="card__description").text().startswith("<img src=x onerror=")
        assert not card.find_all("img") and not card.find_all("b")


async def test_hostile_file_names_are_escaped_in_the_tree_and_every_link_works(
    site, hostile, db_session, blob_store
):
    page = await visit(site, "/registry/skills/xss-lab")

    tree = page.find("section", cls="detail__files")
    files = sorted(a.text() for a in tree.find_all("a"))
    assert files == sorted(
        [
            "SKILL.md",
            "<script>alert(10)<script>.txt",
            '"><img src=x onerror=alert(12)>.md',
            "a'b\"c&d.txt",
        ]
    )
    directories = [s.find("span", cls="tree__name").text() for s in tree.find_all("summary")]
    assert directories == ["<dir onclick=alert(13)>"]
    assert "&lt;script&gt;alert(10)&lt;script&gt;.txt" in page.response.text
    assert "&lt;dir onclick=alert(13)&gt;" in page.response.text
    service = PackageService(db_session, blob_store)
    for link in tree.find_all("a"):
        opened = await visit(site, link.attrs["href"])
        path = opened.find("h1").find("code").text()
        content = await service.read_file(None, "xss-lab", "1.0.0", path)
        assert opened.by_id("file-content").text() == content.content
        assert opened.find("title").text().startswith(path)


async def test_hostile_file_content_is_text_in_the_file_page(site, hostile):
    url = public.file_url("xss-lab", "1.0.0", "<script>alert(10)<script>.txt")

    page = await visit(site, url)

    assert page.by_id("file-content").text() == "<script>alert(11)</script>\n"
    assert "<script>alert(11)" not in page.response.text


async def test_user_input_reflected_in_the_registry_cannot_break_out_of_attributes(site, library):
    payload = "\"><script>alert(1)</script><img src=x onerror=alert(2)> 'x' &amp;"

    page = await visit(site, "/registry", 200)
    hostile_page = await visit(
        site,
        "/registry?" + "&".join(f"{key}={quote(payload)}" for key in ("q", "category", "tag")),
    )

    assert page.by_id("q").attrs["value"] == ""
    field = hostile_page.by_id("q")
    assert set(field.attrs) == {
        "id",
        "type",
        "name",
        "value",
        "placeholder",
        "autocomplete",
        "maxlength",
    }
    assert field.attrs["value"] == payload
    hidden = hostile_page.find("form", role="search").find_all("input", type="hidden")
    assert [(h.attrs["name"], set(h.attrs)) for h in hidden] == [
        ("category", {"type", "name", "value"}),
        ("tag", {"type", "name", "value"}),
    ]
    assert hidden[0].attrs["value"] == payload.lower()
    labels = [
        c.words() for c in hostile_page.find("ul", cls="chips--active").find_all("a", cls="chip")
    ]
    assert any(payload in label for label in labels)
    for link in hostile_page.find_all("a"):
        assert "<" not in link.attrs["href"] and '"' not in link.attrs["href"]
    assert "<script>alert(1)" not in hostile_page.response.text


async def test_hostile_database_rows_are_escaped_in_cards_facets_and_chips(
    site, db_session, actors
):
    tag = "<script>alert(4)</script>"
    category = '"><svg onload=alert(3)>'
    await add_skill(
        db_session,
        actors.owner,
        "evil-row",
        display_name="<img src=x onerror=alert(1)>",
        description="<script>alert(2)</script>",
        category=category,
        tags=(tag, 'x"onmouseover="alert(5)'),
    )

    page = await visit(site, "/registry")
    filtered = await visit(site, f"/registry?tag={quote(tag)}&category={quote(category)}")

    card = by_name(page)["evil-row"]
    assert card.find("a", cls="card__link").text() == "<img src=x onerror=alert(1)>"
    assert card.find("p", cls="card__description").text() == "<script>alert(2)</script>"
    facet_tags = [
        a.find("span", cls="chip__label").text()
        for a in page.by_id("facet-tags").parent.find_all("a")
    ]
    assert set(facet_tags) == {tag, 'x"onmouseover="alert(5)'}
    assert [a.text() for a in page.by_id("facet-category").parent.find_all("a")] == [
        category + " 1"
    ]
    for link in page.by_id("facet-tags").parent.find_all("a"):
        assert unquote(link.attrs["href"].split("tag=")[1]) in facet_tags
    assert card_names(filtered) == ["evil-row"]
    assert tag in " ".join(c.text() for c in filtered.find_all("a", cls="chip--remove"))


async def test_every_response_of_the_site_carries_the_security_headers(
    site, showcase, db_session, blob_store, actors, monkeypatch
):
    await publish(db_session, blob_store, actors.owner, "broken-pkg")
    await blob_store.delete((await VersionService(db_session).get(None, "broken-pkg")).sha256)
    cases = [
        ("/", 200),
        ("/registry", 200),
        ("/registry/", 200),
        ("/registry?kind=nope", 400),
        ("/registry?page=0", 400),
        ("/registry/skills/pdf-tools", 200),
        ("/registry/skills/pdf-tools?version=1.0.0", 200),
        ("/registry/skills/pdf-tools/versions/1.1.0/files/SKILL.md", 200),
        ("/registry/skills/pdf-tools/versions/1.1.0/files/assets/logo.png", 200),
        ("/registry/skills/ghost", 404),
        ("/registry/skills/pdf-tools/versions/1.1.0/files/nope", 404),
        ("/registry/anything/else", 404),
        ("/registry/skills/broken-pkg", 500),
    ]
    for url, status in cases:
        page = await visit(site, url, status)
        assert page.response.headers["content-security-policy"] == CSP
    monkeypatch.setattr(settings.registry, "public_read", False)
    for url in (
        "/registry",
        "/registry/skills/pdf-tools",
        "/registry/skills/pdf-tools/versions/1.1.0/files/SKILL.md",
    ):
        assert (await visit(site, url, 401)).response.headers["content-security-policy"] == CSP
    assert (await visit(site, "/")).response.headers["x-content-type-options"] == "nosniff"


def md(source: str, *files: str) -> Document:
    context = public.MarkdownContext("pkg", "1.0.0", frozenset(files))
    fragment = parse(public.render_html(source, context))
    assert fragment.problems == [], fragment.problems
    assert_inert(fragment, allowed_tags=MARKDOWN_TAGS)
    assert not fragment.find_all("img")
    for link in fragment.find_all("a"):
        target = link.attrs["href"]
        assert target.startswith(
            ("https://", "http://", "mailto:", "/registry/skills/pkg/", "#md-")
        )
    return fragment


MARKDOWN_ATTACKS = [
    ("<script>alert(1)</script>", "<script>alert(1)</script>"),
    ("<img src=x onerror=alert(1)>", "<img src=x onerror=alert(1)>"),
    ('<div onclick="alert(1)">x</div>', '<div onclick="alert(1)">x</div>'),
    ("<iframe src=//evil.example></iframe>", "<iframe src=//evil.example></iframe>"),
    ("<style>*{display:none}</style>", "<style>*{display:none}</style>"),
    ("<a href=javascript:alert(1)>x</a>", "<a href=javascript:alert(1)>x</a>"),
    ("<svg onload=alert(1)>", "<svg onload=alert(1)>"),
    ("<!-- --><script>alert(1)</script>", "<script>alert(1)</script>"),
    ("</div></main><script>alert(1)</script>", "</div></main><script>alert(1)</script>"),
    ("[x](javascript:alert(1))", "[x](javascript:alert(1))"),
    ("[x](JaVaScRiPt:alert(1))", "[x](JaVaScRiPt:alert(1))"),
    ("[x](java&#x73;cript:alert(1))", "[x](javascript:alert(1))"),
    ("[x](&#106;avascript:alert(1))", "[x](javascript:alert(1))"),
    ("[x](vbscript:msgbox(1))", "[x](vbscript:msgbox(1))"),
    ("[x](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)", "data:text/html"),
    ("[x](file:///etc/passwd)", "[x](file:///etc/passwd)"),
    ("[x](ftp://host/file)", "[x](ftp://host/file)"),
    ("[x](ssh://host)", "[x](ssh://host)"),
    ("[x](tel:+123)", "[x](tel:+123)"),
    ("[x](//evil.example/path)", "[x](//evil.example/path)"),
    ("[x]( javascript:alert(1))", "[x]( javascript:alert(1))"),
    ("[x](<javascript:alert(1)>)", "[x](<javascript:alert(1)>)"),
    ("[x][1]\n\n[1]: javascript:alert(1)", "[x][1]"),
    ("<javascript:alert(1)>", "<javascript:alert(1)>"),
    ("![x](javascript:alert(1))", "![x](javascript:alert(1))"),
    ("![x](data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=)", "![x](data:image/svg+xml"),
    ("![x](https://tracker.example/p.png)", "Image: x"),
    ('![x](https://tracker.example/p.png "title")', "Image: x"),
    ("[![badge](https://img.example/b.svg)](https://example.com)", "Image: badge"),
    ('```html" onmouseover="alert(1)\n<b>x</b>\n```', "<b>x</b>"),
    ('[x](https://example.com "t\\" onmouseover=\\"alert(1)")', "x"),
    ("# <b>x</b>", "<b>x</b>"),
    (
        "| <script>alert(1)</script> |\n|---|\n| <img src=x onerror=alert(1)> |",
        "<script>alert(1)</script>",
    ),
    ("&lt;script&gt;alert(1)&lt;/script&gt;", "<script>alert(1)</script>"),
    ("*<script>*alert(1)*</script>*", "<script>"),
    ('[x](https://example.com/"><script>alert(1)</script>)', "x"),
    ("[x](mailto:a@b.c?subject=<script>)", "x"),
    ("[x](/\\evil.example)", "x"),
    ("[x](\\\\evil.example)", "x"),
    ("[x](docs/guide.md)", "x"),
    ("[x](#missing)", "x"),
    ("[x](<java\tscript:alert(1)>)", "x"),
    ("[x](java\nscript:alert(1))", "x"),
]


@pytest.mark.parametrize(("source", "shown"), MARKDOWN_ATTACKS)
def test_markdown_attacks_end_up_as_inert_text(source, shown):
    fragment = md(source)

    assert shown in fragment.text()
    assert not fragment.find_all("script")
    assert not [a for a in fragment.find_all("a") if "javascript" in a.attrs["href"].lower()]


def test_markdown_never_produces_raw_html_blocks_or_inline_html():
    fragment = md(
        "<b>bold</b> and <i>it</i>\n\n<p>para</p>\n\n<details open><summary>s</summary></details>"
    )

    assert not fragment.find_all("b") and not fragment.find_all("i")
    assert not fragment.find_all("details")
    assert "<b>bold</b>" in fragment.text()


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("[a](docs/guide.md)", "docs/guide.md"),
        ("[a](./docs/../docs/guide.md)", "docs/guide.md"),
        ("[a](docs/guide.md#frag)", "docs/guide.md"),
        ("[a](docs/guide.md?x=1)", "docs/guide.md"),
        ("[a](docs//guide.md)", "docs/guide.md"),
        ("[a](docs/guide%20two.md)", "docs/guide two.md"),
        ("[a](<docs/guide two.md>)", "docs/guide two.md"),
        ("[a](docs/%67uide.md)", "docs/guide.md"),
        ("[a](a/../docs/guide.md)", "docs/guide.md"),
    ],
)
def test_relative_links_resolve_to_files_of_the_package(source, expected):
    fragment = md(source, "docs/guide.md", "docs/guide two.md")

    link = fragment.find("a")
    assert link.attrs["href"] == public.file_url("pkg", "1.0.0", expected)


@pytest.mark.parametrize(
    "source",
    [
        "[a](docs/GUIDE.md)",
        "[a](docs/)",
        "[a](docs)",
        "[a](../outside.md)",
        "[a](docs/../../outside.md)",
        "[a](/docs/guide.md)",
        "[a](</docs/guide.md>)",
        "[a](docs\\guide.md)",
        "[a](nothing.md)",
        "[a](%2e%2e/docs/guide.md)",
        "[a](docs/guide.md%00)",
        "[a](#nothing)",
        "[a](#)",
        "[a]()",
        "[a](<>)",
    ],
)
def test_links_to_files_that_do_not_exist_in_the_package_become_plain_text(source):
    fragment = md(source, "docs/guide.md")

    assert not fragment.find_all("a")
    assert fragment.find("p").words() == "a"


def test_headings_are_shifted_below_the_page_title_and_get_unique_anchors():
    fragment = md(
        "# One\n\n## Two\n\n### Three\n\n#### Four\n\n##### Five\n\n###### Six\n\n"
        "Setext\n======\n\n# One\n\n# Caf" + chr(0xE9) + " Menu!"
    )

    headings = [
        (h.tag, h.words(), h.attrs["id"])
        for h in fragment.find_all()
        if re.fullmatch(r"h\d", h.tag)
    ]
    assert headings == [
        ("h2", "One", "md-one"),
        ("h3", "Two", "md-two"),
        ("h4", "Three", "md-three"),
        ("h5", "Four", "md-four"),
        ("h6", "Five", "md-five"),
        ("h6", "Six", "md-six"),
        ("h2", "Setext", "md-setext"),
        ("h2", "One", "md-one-1"),
        ("h2", "Caf" + chr(0xE9) + " Menu!", "md-caf" + chr(0xE9) + "-menu"),
    ]
    assert not fragment.find_all("h1")


def test_fragment_links_point_to_the_generated_heading_anchors():
    fragment = md(
        "# Getting Started\n\n# Getting Started\n\n# Caf" + chr(0xE9) + "\n\n"
        "[a](#getting-started) [b](#Getting-Started) [c](#getting-started-1) "
        "[d](#caf%C3%A9) [e](#missing)"
    )

    links = {a.words(): a.attrs["href"] for a in fragment.find_all("a")}
    assert links == {
        "a": "#md-getting-started",
        "b": "#md-getting-started",
        "c": "#md-getting-started-1",
        "d": "#md-caf%C3%A9",
    }


def test_external_links_carry_nofollow_and_never_open_new_windows():
    fragment = md("[a](https://example.com) [b](http://example.com) <https://auto.example>")

    anchors = fragment.find_all("a")
    assert len(anchors) == 3
    for anchor in anchors:
        assert anchor.attrs["rel"] == "nofollow ugc noreferrer"
        assert "target" not in anchor.attrs


def test_images_are_replaced_by_their_alt_text_and_never_requested():
    fragment = md("![A *nice* `chart`](https://example.com/c.png) ![](x.png) ![ ](y.png)")

    labels = [s.words() for s in fragment.find_all("span", cls="md-image")]
    assert labels == ["Image: A nice chart", "Image", "Image"]


def test_tables_have_scrollable_wrappers_alignment_classes_and_no_inline_styles():
    fragment = md("| a | b | c | d |\n|:--|:-:|--:|---|\n| 1 | 2 | 3 | 4 |")

    cells = fragment.find_all("th") + fragment.find_all("td")
    assert [c.classes for c in cells] == [
        ["align-left"],
        ["align-center"],
        ["align-right"],
        [],
    ] * 2
    assert not [n for n in fragment.walk() if "style" in n.attrs]
    assert fragment.find("div", cls="md-table").attrs["tabindex"] == "0"


def test_code_blocks_are_focusable_and_escape_their_content():
    fragment = md("    indented <b>code</b>\n\n```\nfence <i>code</i>\n```\n\n`inline <u>`")

    blocks = fragment.find_all("pre")
    assert [b.attrs["tabindex"] for b in blocks] == ["0", "0"]
    assert [b.text() for b in blocks] == ["indented <b>code</b>\n", "fence <i>code</i>\n"]
    assert fragment.find("p").find("code").text() == "inline <u>"
    assert not fragment.find_all("b") and not fragment.find_all("u")


def test_the_renderer_keeps_titles_but_drops_unknown_link_attributes():
    fragment = md('[a](https://example.com "A title")')

    assert set(fragment.find("a").attrs) == {"href", "title", "rel"}
    assert fragment.find("a").attrs["title"] == "A title"


async def test_rendering_runs_off_the_event_loop_and_is_cached_by_content(monkeypatch):
    threads: list[str] = []
    original = public.render_html

    def spy(text, context):
        threads.append(threading.current_thread().name)
        return original(text, context)

    monkeypatch.setattr(public, "render_html", spy)
    context = public.MarkdownContext("pkg", "1.0.0", frozenset())

    first = await public.render_markdown("# Same", context)
    second = await public.render_markdown("# Same", context)

    assert first == second and first is second
    assert len(threads) == 1 and threads[0] != threading.main_thread().name
    other = await public.render_markdown(
        "# Same", public.MarkdownContext("pkg", "1.0.1", frozenset())
    )
    assert other is not first and len(threads) == 2
    files = await public.render_markdown(
        "# Same", public.MarkdownContext("pkg", "1.0.0", frozenset({"a"}))
    )
    assert files is not first and len(threads) == 3


async def test_the_markdown_cache_is_bounded_and_evicts_the_least_recently_used(monkeypatch):
    monkeypatch.setattr(public, "MARKDOWN_CACHE_SIZE", 2)
    context = public.MarkdownContext("pkg", "1.0.0", frozenset())
    one = await public.render_markdown("one", context)
    await public.render_markdown("two", context)
    assert await public.render_markdown("one", context) is one
    await public.render_markdown("three", context)

    assert len(public.MARKDOWN_CACHE) == 2
    keys = list(public.MARKDOWN_CACHE)
    assert keys[-1] == public.fingerprint("three", context)
    assert public.fingerprint("one", context) in keys
    assert public.fingerprint("two", context) not in keys


@pytest.mark.parametrize(
    "source",
    [
        "[" * 20000,
        "![" * 10000,
        "]" * 20000,
        "(" * 20000,
        "[a](" * 5000,
        "<" * 20000,
        ">" * 5000 + " x",
        "*" * 20000,
        "_a" * 10000,
        "`" * 20000,
        "".join("  " * i + "- x\n" for i in range(200)),
        "".join("> " * i + "x\n" for i in range(100)),
        "# " + "a" * 100000,
        "|a|b|\n|-|-|\n" + "|1|2|\n" * 3000,
        "[x]: http://e.com\n" * 2000 + "[x]",
        "&" * 20000,
        "\\" * 20000,
    ],
)
def test_pathological_markdown_renders_without_errors(source):
    fragment = md(source)

    assert fragment.children is not None


def test_random_markdown_is_always_inert():
    pieces = [
        "[",
        "]",
        "(",
        ")",
        "<",
        ">",
        "!",
        "*",
        "_",
        "`",
        "~",
        "#",
        "|",
        "-",
        "\n",
        "\n\n",
        " ",
        "javascript:",
        "data:",
        "https://e.org/",
        "x",
        "alert(1)",
        "&",
        "&#x6a;",
        "\\",
        '"',
        "'",
        "</",
        "script",
        "img",
        "onerror=",
        "```",
        "    ",
        "> ",
        "1. ",
        "- ",
        ":|",
        "---",
        "<https://a.b>",
        "[x](",
        "](",
        "docs/guide.md",
        "=",
        "vbscript:",
        "file:",
        "//",
        "%00",
        "http:",
        "mailto:a@b.c",
        "<mailto:a@b.c>",
        "![",
        "[]",
        "()",
    ]
    generator = random.Random(2026)
    for _ in range(600):
        source = "".join(generator.choice(pieces) for _ in range(generator.randint(3, 80)))
        md(source, "docs/guide.md")


def test_frontmatter_is_removed_only_when_it_is_a_complete_block_at_the_start():
    body = "# Title\n\nText\n"
    assert public.strip_frontmatter(f"---\nname: x\n---\n{body}") == body
    assert public.strip_frontmatter(f"---\r\nname: x\r\n---\r\n{body}") == body
    assert public.strip_frontmatter(f"{chr(0xFEFF)}---\nname: x\n---\n{body}") == body
    assert public.strip_frontmatter(f"---  \nname: x\n---  \n{body}") == body
    assert public.strip_frontmatter("---\nname: x\n---") == ""
    assert public.strip_frontmatter(body) == body
    unclosed = "---\nname: x\n\n# Heading\n"
    assert public.strip_frontmatter(unclosed) == unclosed
    assert (
        public.strip_frontmatter(f"text\n---\na: b\n---\n{body}") == f"text\n---\na: b\n---\n{body}"
    )
    assert public.strip_frontmatter("") == ""
    assert public.strip_frontmatter(f"---\n---\n{body}") == body
    assert public.strip_frontmatter(f"---\na: b\n----\n---\n{body}") == body


LEGACY_FRONTMATTER_OPEN = re.compile(rf"{chr(0xFEFF)}?---[ \t]*\r?\n")
LEGACY_FRONTMATTER_CLOSE = re.compile(r"^---[ \t]*(?:\r?\n|\Z)", re.MULTILINE)


def legacy_strip_frontmatter(text: str) -> str:
    opening = LEGACY_FRONTMATTER_OPEN.match(text)
    if opening is None:
        return text
    closing = LEGACY_FRONTMATTER_CLOSE.search(text, opening.end())
    return text if closing is None else text[closing.end() :]


def test_the_site_cuts_the_frontmatter_with_the_function_of_the_inspector(monkeypatch):
    seen: list[str] = []

    def spy(text: str) -> tuple[str, str]:
        seen.append(text)
        return "block", "rest"

    monkeypatch.setattr(public, "split_frontmatter", spy)

    assert public.strip_frontmatter(f"{chr(0xFEFF)}---\nname: x\n---\nbody") == "rest"
    assert public.strip_frontmatter("anything") == "rest"
    assert seen == ["---\nname: x\n---\nbody", "anything"]


def test_the_site_keeps_no_copy_of_the_frontmatter_rules():
    assert not hasattr(public, "FRONTMATTER_OPEN")
    assert not hasattr(public, "FRONTMATTER_CLOSE")


def test_the_site_returns_the_text_untouched_when_the_inspector_would_reject_it(monkeypatch):
    def refuse(text: str) -> tuple[str, str]:
        raise InvalidPackageError("BAD_FRONTMATTER")

    monkeypatch.setattr(public, "split_frontmatter", refuse)

    assert public.strip_frontmatter("---\na: b\n---\nbody") == "---\na: b\n---\nbody"


def test_a_block_over_the_inspector_limit_is_not_cut_because_no_package_can_have_one():
    block = "#" * (16 * 1024) + "\n"
    text = f"---\n{block}---\nbody"

    assert public.strip_frontmatter(text) == text
    assert legacy_strip_frontmatter(text) == "body"


def test_the_site_cuts_exactly_where_the_old_rules_did_for_any_block_within_the_limit():
    pieces = [
        "---",
        "---\n",
        "--- \n",
        "---\r\n",
        "----\n",
        "\n",
        "\r\n",
        " ",
        "\t",
        chr(0xFEFF),
        "a: b\n",
        "text",
        "...\n",
        "- --\n",
        "\u20ac",
        "# Title\n",
    ]
    generator = random.Random(20261016)
    checked = 0
    for _ in range(4000):
        text = "".join(generator.choice(pieces) for _ in range(generator.randint(0, 14)))
        assert public.strip_frontmatter(text) == legacy_strip_frontmatter(text), repr(text)
        checked += 1
    assert checked == 4000


def test_long_documents_are_cut_on_a_line_boundary_and_flagged():
    lines = "".join(f"line {i}\n" for i in range(20000))

    short, clipped = public.clip_markdown("tiny\n")
    cut, was_cut = public.clip_markdown(lines)
    blob, blob_cut = public.clip_markdown("x" * (public.MARKDOWN_LIMIT * 2))

    assert (short, clipped) == ("tiny\n", False)
    assert was_cut and len(cut) <= public.MARKDOWN_LIMIT and lines.startswith(cut)
    assert re.fullmatch(r"line \d+", cut.splitlines()[-1])
    assert blob_cut and len(blob) == public.MARKDOWN_LIMIT
    exact, exact_cut = public.clip_markdown("y" * public.MARKDOWN_LIMIT)
    assert not exact_cut and len(exact) == public.MARKDOWN_LIMIT


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "0 B"),
        (1, "1 B"),
        (1023, "1023 B"),
        (1024, "1.0 KiB"),
        (1536, "1.5 KiB"),
        (10 * 1024, "10 KiB"),
        (1024 * 1000, "1000 KiB"),
        (1024**2, "1.0 MiB"),
        (25 * 1024**2, "25 MiB"),
        (1024**3, "1.0 GiB"),
        (5000 * 1024**3, "5000 GiB"),
    ],
)
def test_sizes_are_shown_in_binary_units(value, expected):
    assert public.human_size(value) == expected


def test_small_formatting_helpers():
    assert public.short_sha("a" * 64) == "a" * 12
    assert public.iso_date(None) == "-"
    assert public.iso_date(datetime(2026, 3, 4, 23, 30, tzinfo=UTC)) == "2026-03-04"
    assert (
        public.iso_date(datetime(2026, 3, 4, 23, 30, tzinfo=timezone(timedelta(hours=5))))
        == "2026-03-04"
    )
    assert (
        public.iso_date(datetime(2026, 3, 4, 2, 0, tzinfo=timezone(timedelta(hours=5))))
        == "2026-03-03"
    )
    assert public.plural(1, "entry", "entries") == "1 entry"
    assert public.plural(0, "entry", "entries") == "0 entries"
    assert public.plural(2, "file", "files") == "2 files"


def test_urls_are_built_from_names_and_quoted_per_segment():
    assert public.skill_url("pdf-tools") == "/registry/skills/pdf-tools"
    assert public.skill_url("a/b c") == "/registry/skills/a%2Fb%20c"
    assert public.version_url("x", "1.0.0+b") == "/registry/skills/x?version=1.0.0%2Bb"
    assert public.file_url("x", "1.0.0", "a b/c#d?e%f.txt") == (
        "/registry/skills/x/versions/1.0.0/files/a%20b/c%23d%3Fe%25f.txt"
    )
    assert public.raw_url("x", "1.0.0", "d/" + chr(0xE9) + ".txt") == (
        "/api/v1/skills/x/versions/1.0.0/raw/d/%C3%A9.txt"
    )
    assert public.download_path("x", "1.0.0") == "/api/v1/skills/x/versions/1.0.0/download"
    assert public.download_path("x", "latest") == "/api/v1/skills/x/versions/latest/download"


def test_shell_snippets_quote_hostile_names_and_versions():
    text = public.use_snippets("a;rm -rf ~", "1.0.0$(id)", "f" * 64)

    mcp, curl, alx = (item["text"] for item in text)
    assert "'a;rm -rf ~-1.0.0$(id).zip'" in curl
    assert (
        "'https://registry.example.org/api/v1/skills/a%3Brm%20-rf%20~/versions/1.0.0%24%28id%29/download'"
        in curl
    )
    assert alx.splitlines()[-1] == "alx pull 'a;rm -rf ~@1.0.0$(id)'"
    assert json.loads(mcp.removeprefix("get_entrypoint ")) == {
        "name": "a;rm -rf ~",
        "version": "1.0.0$(id)",
    }
    landing = public.connect_snippets("a;rm -rf ~", False)
    assert "'a;rm -rf ~'" in landing["alx"]
    assert "a%3Brm%20-rf%20~" in landing["entrypoint"]
    assert shlex.split(landing["download"])[2].endswith("/versions/latest/download")


def test_the_tree_is_sorted_with_directories_first_and_caps_its_depth():
    def entry(path: str, size: int = 1) -> ManifestEntryResponse:
        return ManifestEntryResponse(
            path=path, size=size, sha256="0" * 64, media_type="text/plain", text=True
        )

    deep = "/".join(f"d{i}" for i in range(9))
    files = [
        entry(p)
        for p in (
            "b.txt",
            "A.txt",
            "z/last.txt",
            "a/first.txt",
            "a/sub/inner.txt",
            f"{deep}/leaf.txt",
            "SKILL.md",
        )
    ]

    tree = public.build_tree(files, "a/sub/inner.txt", "SKILL.md")

    assert [(n.name, n.is_dir) for n in tree] == [
        ("a", True),
        ("d0", True),
        ("z", True),
        ("A.txt", False),
        ("b.txt", False),
        ("SKILL.md", False),
    ]
    first = tree[0]
    assert first.count == 2 and first.open
    assert [(c.name, c.is_dir) for c in first.children] == [("sub", True), ("first.txt", False)]
    assert first.children[0].children[0].current
    assert [n.entrypoint for n in tree if not n.is_dir] == [False, False, True]
    node = tree[1]
    labels = []
    while node.children:
        node = node.children[0]
        labels.append(node.name)
    assert labels == ["d1", "d2", "d3", "d4", "d5", "d6/d7/d8/leaf.txt"]
    assert len(labels) == public.TREE_DEPTH


def test_large_trees_start_collapsed_except_the_top_level_and_the_current_branch():
    def entry(path: str) -> ManifestEntryResponse:
        return ManifestEntryResponse(
            path=path, size=1, sha256="0" * 64, media_type="text/plain", text=True
        )

    files = [entry(f"top/mid{i}/leaf{j}.txt") for i in range(10) for j in range(5)]

    tree = public.build_tree(files, "top/mid3/leaf1.txt", "")

    top = tree[0]
    assert top.open
    assert [child.open for child in top.children] == [i == 3 for i in range(10)]
    assert [child.name for child in top.children] == [f"mid{i}" for i in range(10)]


@pytest.mark.parametrize(
    ("error", "status", "title"),
    [
        (UnauthorizedError(), 401, "This registry is private"),
        (ForbiddenError("no"), 403, "Not allowed"),
        (NotFoundError("gone"), 404, "Not found"),
        (ValidationError("bad"), 400, "Invalid request"),
        (ValidationError("bad path", reason="INVALID_PATH"), 404, "Not found"),
        (ConflictError("clash"), 500, "Something went wrong"),
        (StorageError("disk /var/secret broke"), 500, "Something went wrong"),
        (DomainError("other"), 500, "Something went wrong"),
    ],
)
async def test_domain_errors_become_site_error_pages_with_the_right_status(error, status, title):
    application = FastAPI()
    router = APIRouter(route_class=public.SiteRoute)

    @router.get("/boom")
    async def boom():
        raise error

    application.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://t") as client:
        page = await visit(client, "/boom", status)

    assert page.find("h1").words() == title
    lead = page.find("p", cls="lead").words()
    if status in (401, 403, 500):
        assert lead == public.ERROR_FIXED_TEXT[status]
        assert "secret" not in page.response.text and "clash" not in page.response.text
    else:
        assert lead == error.message
    assert ("www-authenticate" in page.response.headers) == (status == 401)


async def test_unexpected_errors_are_not_swallowed_by_the_site_routes():
    application = FastAPI()
    router = APIRouter(route_class=public.SiteRoute)

    @router.get("/bug")
    async def bug():
        raise RuntimeError("a real bug")

    application.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://t") as client:
        with pytest.raises(RuntimeError, match="a real bug"):
            await client.get("/bug")


def test_the_templates_environment_escapes_everything_and_rejects_undefined_variables():
    environment = public.templates.env

    assert environment.autoescape is True
    assert environment.from_string("{{ value }}").render(value="<b>&\"'") == (
        "&lt;b&gt;&amp;&#34;&#39;"
    )
    with pytest.raises(jinja2.UndefinedError):
        environment.from_string("{{ missing }}").render()


def template_paths() -> list[Path]:
    return sorted(public.TEMPLATES_DIR.rglob("*.html"))


@pytest.mark.parametrize("path", template_paths(), ids=lambda path: path.name)
def test_templates_have_no_inline_code_external_resources_or_unescaped_output(path):
    source = path.read_text(encoding="utf-8")

    assert "<style" not in source
    assert not re.search(r"\sstyle\s*=", source)
    assert not re.search(r"<\s*(img|iframe|object|embed|base|link\s+rel=.preload)", source)
    assert not re.search(r"\son[a-z]+\s*=", source, re.IGNORECASE)
    assert "javascript:" not in source.lower()
    assert "|safe" not in source and "| safe" not in source
    assert "autoescape" not in source and "{% raw" not in source
    assert "{#" not in source and "<!--" not in source
    assert not re.search(r"(https?:)?//[a-z0-9.-]+\.[a-z]{2,}", source, re.IGNORECASE)
    scripts = re.findall(r"<script[^>]*>", source)
    if path.name == "base.html":
        assert scripts == ["<script src=\"{{ asset('site.js') }}\" defer>"]
    else:
        assert scripts == []
    assert "<script" not in re.sub(r"<script[^>]*></script>", "", source)


def test_the_stylesheet_is_self_contained_and_covers_the_required_modes():
    css = (public.STATIC_DIR / "style.css").read_text(encoding="utf-8")

    assert "@import" not in css and "url(" not in css.lower()
    assert "@font-face" not in css and "//" not in css.replace("/*", "").replace("*/", "")
    assert "/*" not in css
    assert "@media (prefers-color-scheme: dark)" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert "@media (forced-colors: active)" in css
    assert ":focus-visible" in css and "outline: 3px solid var(--focus)" in css
    assert "color-scheme: light dark" in css
    assert css.count("!important") <= 6
    assert re.search(r"@media \(min-width: [\d.]+rem\)", css)
    assert "system-ui" in css and "ui-monospace" in css
    assert re.search(r"h1,\s*h2,\s*h3\s*\{[^}]*overflow-wrap:\s*anywhere", css)
    assert re.search(r"\.chip\s*\{[^}]*max-width:\s*100%", css)


def test_the_script_is_a_small_progressive_enhancement_without_network_or_html_injection():
    script = (public.STATIC_DIR / "site.js").read_text(encoding="utf-8")

    for forbidden in (
        "eval(",
        "new Function",
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "XMLHttpRequest",
        "fetch(",
        "WebSocket",
        "sendBeacon",
        "localStorage",
        "sessionStorage",
        "document.cookie",
        "import(",
        "importScripts",
        "location",
        "//",
        "/*",
    ):
        assert forbidden not in script, forbidden
    assert len(script.splitlines()) < 100
    assert 'querySelectorAll("button[data-copy]")' in script
    assert "navigator.clipboard" in script


def test_the_favicon_is_a_self_contained_svg_without_scripts_or_links():
    icon = (public.STATIC_DIR / "favicon.svg").read_text(encoding="utf-8")

    assert icon.startswith("<svg")
    for forbidden in ("<script", "href", "onload", "<image", "<foreignObject", "url("):
        assert forbidden not in icon


def hex_tokens(css: str) -> list[dict[str, str]]:
    blocks = re.findall(r":root\s*\{([^}]*)\}", css)
    return [dict(re.findall(r"--([a-z-]+):\s*(#[0-9a-fA-F]{6})\s*;", block)) for block in blocks]


def luminance(color: str) -> float:
    channels = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(first: str, second: str) -> float:
    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


TEXT_PAIRS = [
    ("text", "bg"),
    ("text", "surface"),
    ("text", "sunk"),
    ("text", "code-bg"),
    ("muted", "bg"),
    ("muted", "surface"),
    ("muted", "sunk"),
    ("muted", "code-bg"),
    ("faint", "bg"),
    ("faint", "surface"),
    ("faint", "sunk"),
    ("faint", "accent-bg"),
    ("accent", "bg"),
    ("accent", "surface"),
    ("accent", "sunk"),
    ("accent", "accent-bg"),
    ("accent-hover", "bg"),
    ("accent-hover", "surface"),
    ("on-accent", "accent"),
    ("on-accent", "accent-hover"),
    ("skill", "skill-bg"),
    ("skill", "surface"),
    ("grounding", "grounding-bg"),
    ("grounding", "surface"),
    ("ok", "ok-bg"),
    ("warn", "warn-bg"),
    ("text", "info-bg"),
    ("text", "warn-bg"),
    ("accent", "info-bg"),
    ("accent", "warn-bg"),
]
GRAPHIC_PAIRS = [
    ("border", "bg"),
    ("border", "surface"),
    ("focus", "bg"),
    ("focus", "surface"),
    ("focus", "sunk"),
    ("info", "surface"),
    ("warn", "surface"),
]


@pytest.mark.parametrize("mode", [0, 1], ids=["light", "dark"])
def test_every_color_pair_meets_wcag_aa(mode):
    tokens = hex_tokens((public.STATIC_DIR / "style.css").read_text(encoding="utf-8"))
    palette = tokens[0] if mode == 0 else {**tokens[0], **tokens[1]}

    assert len(tokens) == 2
    for foreground, background in TEXT_PAIRS:
        ratio = contrast(palette[foreground], palette[background])
        assert ratio >= 4.5, (foreground, background, round(ratio, 2))
    for foreground, background in GRAPHIC_PAIRS:
        ratio = contrast(palette[foreground], palette[background])
        assert ratio >= 3.0, (foreground, background, round(ratio, 2))


def test_the_site_module_follows_the_project_code_rules():
    path = Path(public.__file__)
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    comments = [
        token.string
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT
    ]
    documented = [
        getattr(node, "name", "<module>")
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and ast.get_docstring(node, clean=False) is not None
    ]
    assert comments == [] and documented == []
    assert source.count("Markup(") == 1
    assert all(ord(character) < 127 for character in source)


def test_the_site_only_talks_to_the_service_layer():
    tree = ast.parse(Path(public.__file__).read_text(encoding="utf-8"))

    imported = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not {m for m in imported if m.startswith("app.repositories")}
    assert not {m for m in imported if m.startswith("app.models") and m != "app.models.schemas"}
    assert "sqlalchemy" not in imported and "sqlalchemy.orm" not in imported
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert not attributes & {"execute", "commit", "rollback", "scalars", "flush"}
    assert not [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Raise)
        and n.exc is not None
        and getattr(getattr(n.exc, "func", n.exc), "id", "") in {"HTTPException", "ValueError"}
    ]


def test_the_templates_directory_has_exactly_the_documented_pages():
    names = {path.name for path in template_paths()}

    assert names == {
        "base.html",
        "macros.html",
        "index.html",
        "registry.html",
        "skill_detail.html",
        "file.html",
        "error.html",
    }
    assets = {path.name for path in public.STATIC_DIR.iterdir()}
    assert assets == {"style.css", "site.js", "favicon.svg"}


def test_the_asset_url_falls_back_to_the_plain_path_when_the_file_is_missing(monkeypatch, tmp_path):
    assert re.fullmatch(
        rf"{public.STATIC_URL}/style\.css\?v=[0-9a-f]+", public.asset_url("style.css")
    )

    monkeypatch.setattr(public, "STATIC_DIR", tmp_path)

    assert public.asset_url("missing.css") == f"{public.STATIC_URL}/missing.css"


def test_the_link_rewriter_refuses_every_scheme_except_the_allowed_ones():
    context = public.MarkdownContext("pkg", "1.0.0", frozenset({"a.md"}))

    for href in (
        "javascript:alert(1)",
        "JAVASCRIPT:alert(1)",
        "data:text/html,x",
        "vbscript:x",
        "file:///x",
        "ftp://host",
        "ssh://host",
        "tel:1",
        "blob:https://x",
        "custom-app:open",
        "http:",
        "https:",
        "http:alert(1)",
        "http://",
        "https:///path",
        "https://?x",
        "mailto:",
        "mailto: a@b.c",
        "",
    ):
        assert public.link_attrs(href, context, set()) is None
    assert public.link_attrs("https://e.org", context, set()) == {
        "href": "https://e.org",
        "rel": "nofollow ugc noreferrer",
    }
    assert public.link_attrs("HTTP://e.org", context, set())["rel"] == "nofollow ugc noreferrer"
    assert public.link_attrs("mailto:a@b.c", context, set())["href"] == "mailto:a@b.c"
    assert public.link_attrs("a.md", context, set()) == {
        "href": public.file_url("pkg", "1.0.0", "a.md")
    }
    assert public.link_attrs("#x", context, {"x"}) == {"href": "#md-x"}
    assert public.link_attrs("#x", context, set()) is None
    assert public.link_attrs(None, context, set()) is None


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://e.org/a", True),
        ("http://e.org", True),
        ("mailto:a@b.c", True),
        ("MAILTO:a@b.c", True),
        ("docs/guide.md", True),
        ("#anchor", True),
        ("/absolute", True),
        ("", True),
        ("javascript:alert(1)", False),
        ("JaVaScRiPt:alert(1)", False),
        ("data:text/html,x", False),
        ("vbscript:x", False),
        ("file:///etc/passwd", False),
        ("ftp://h/x", False),
        ("//evil.example/x", False),
        ("\\\\evil.example", False),
        ("/\\evil.example", False),
        ("  javascript:alert(1)", False),
        ("tel:+1", False),
    ],
)
def test_the_link_validator_only_lets_through_safe_schemes_and_relative_urls(url, allowed):
    assert public.safe_link(url) is allowed


def test_long_metadata_values_are_cut_and_the_list_is_bounded():
    items = public.metadata_items(
        {"long": "x" * 1000, "number": 7, "flag": True, "none": None, "list": [1, "a"], "text": "t"}
    )

    assert dict(items)["long"] == "x" * public.MAX_METADATA_LENGTH + "..."
    assert dict(items)["number"] == "7" and dict(items)["flag"] == "true"
    assert dict(items)["none"] == "null" and dict(items)["list"] == '[1, "a"]'
    assert [key for key, _ in items] == sorted(["long", "number", "flag", "none", "list", "text"])
    many = public.metadata_items(
        {f"key{i:03d}": "v" for i in range(public.MAX_METADATA_ITEMS + 10)}
    )
    assert len(many) == public.MAX_METADATA_ITEMS


async def test_a_valid_file_view_does_read_the_blob_so_the_hostile_cases_prove_something(
    site, showcase, spy
):
    spy.reads.clear()

    await visit(site, "/registry/skills/pdf-tools/versions/1.1.0/files/scripts/run.py")

    assert len(spy.reads) == 1
    spy.reads.clear()
    await visit(site, "/registry/skills/pdf-tools")
    assert len(spy.reads) == 1


async def test_the_detail_page_reads_the_blob_once_however_often_it_is_viewed(site, showcase, spy):
    spy.reads.clear()

    first = await visit(site, "/registry/skills/pdf-tools")
    second = await visit(site, "/registry/skills/pdf-tools")
    third = await visit(site, "/registry/skills/pdf-tools?version=1.1.0")

    assert len(spy.reads) == 1
    assert first.find("h1").words() == second.find("h1").words() == third.find("h1").words()
    assert first.response.text == second.response.text


async def test_a_file_page_reads_the_blob_once_however_often_it_is_viewed(site, showcase, spy):
    url = "/registry/skills/pdf-tools/versions/1.1.0/files/scripts/run.py"
    spy.reads.clear()

    first = await visit(site, url)
    second = await visit(site, url)

    assert len(spy.reads) == 1
    assert first.response.text == second.response.text
    other = await visit(site, "/registry/skills/pdf-tools/versions/1.1.0/files/SKILL.md")
    assert len(spy.reads) == 2
    assert other.response.status_code == 200


async def test_the_entry_point_viewed_as_a_page_and_as_a_file_is_read_once(site, showcase, spy):
    spy.reads.clear()

    await visit(site, "/registry/skills/pdf-tools?version=1.1.0")
    await visit(site, "/registry/skills/pdf-tools/versions/1.1.0/files/SKILL.md")

    assert len(spy.reads) == 1


GOOD_PAGE = '<html lang="en"><body><a href="#main">Skip</a><main id="main"><h1>T</h1>{}</main></body></html>'


@pytest.mark.parametrize(
    "markup",
    [
        "<script>alert(1)</script>",
        '<script src="https://evil.example/x.js"></script>',
        '<script src="/static/site.js">alert(1)</script>',
        '<script src="/static/other.js"></script>',
        "<img src=x>",
        '<a href="javascript:alert(1)">x</a>',
        '<a href=" jAvA&#9;ScRiPt:alert(1)">x</a>',
        '<a href="data:text/html,x">x</a>',
        '<a href="ftp://host/file">x</a>',
        '<a href="relative/path">x</a>',
        '<p onclick="x">x</p>',
        '<p ONMOUSEOVER="x">x</p>',
        '<p style="color:red">x</p>',
        "<iframe></iframe>",
        "<object></object>",
        "<embed>",
        "<style></style>",
        "<base href='/'>",
        '<meta http-equiv="refresh" content="0;url=/">',
        '<link rel="preload" href="/x">',
        '<link rel="stylesheet" href="javascript:x">',
        '<form action="javascript:x"></form>',
        '<button formaction="/x">x</button>',
        '<div srcdoc="x"></div>',
        "<video></video>",
    ],
)
def test_the_inertness_checker_detects_hostile_markup(markup):
    with pytest.raises(AssertionError):
        assert_inert(parse(GOOD_PAGE.format(markup)))


def test_the_inertness_checker_accepts_the_markup_the_site_really_uses():
    assert_inert(
        parse(
            GOOD_PAGE.format(
                '<a href="/registry?tag=x">a</a><a href="#top">b</a><a href="https://e.org">c</a>'
                '<link rel="stylesheet" href="/static/style.css?v=1">'
                '<script src="/static/site.js?v=2" defer></script>'
            )
        )
    )


@pytest.mark.parametrize(
    "page",
    [
        GOOD_PAGE.replace('lang="en"', ""),
        GOOD_PAGE.format("<h1>U</h1>"),
        GOOD_PAGE.replace("<h1>T</h1>", "<h2>T</h2>").format(""),
        GOOD_PAGE.format("<h3>x</h3>"),
        GOOD_PAGE.format('<input id="a">'),
        GOOD_PAGE.format('<a href="/x"></a>'),
        GOOD_PAGE.format("<svg></svg>"),
        GOOD_PAGE.format("<nav></nav>"),
        GOOD_PAGE.format('<p id="main"></p>'),
        GOOD_PAGE.format("<button></button>"),
        GOOD_PAGE.format("<table><tr><th>x</th></tr></table>"),
        GOOD_PAGE.format("<details><p>x</p></details>"),
        GOOD_PAGE.format("<time>x</time>"),
        GOOD_PAGE.format('<p aria-labelledby="nowhere"></p>'),
        GOOD_PAGE.replace('<main id="main">', "<main>").format(""),
        GOOD_PAGE.replace('<a href="#main">Skip</a>', '<a href="/x">Skip</a>').format(""),
    ],
)
def test_the_accessibility_checker_detects_problems(page):
    with pytest.raises(AssertionError):
        assert_accessible(parse(page))


def test_the_accessibility_checker_accepts_a_well_formed_page():
    assert_accessible(
        parse(
            GOOD_PAGE.format(
                '<h2 id="x">a</h2><p aria-labelledby="x"></p><label for="q">q</label><input id="q">'
                '<input type="hidden" name="h"><nav aria-label="n"></nav><svg aria-hidden="true"></svg>'
                '<table><tr><th scope="col">c</th></tr></table><time datetime="2026-01-01">d</time>'
                '<details><summary>s</summary></details><a href="/x">x</a><button>b</button>'
            )
        )
    )


async def test_a_huge_manifest_is_shown_only_in_part_with_a_link_to_the_full_listing(
    site, showcase, monkeypatch
):
    monkeypatch.setattr(public, "TREE_LIMIT", 3)

    page = await visit(site, "/registry/skills/pdf-tools")

    tree = page.find("section", cls="detail__files")
    assert len(tree.find_all("li", cls="tree__file")) == 3
    note = tree.find("p", cls="tree__note")
    assert note.words() == "Showing 3 of 12 files. Full manifest (JSON)"
    assert note.find("a").attrs["href"] == "/api/v1/skills/pdf-tools/versions/1.1.0/files"
    assert tree.find("h2").words() == "Files 12"
    complete = await visit(site, "/registry/skills/pdf-tools?version=1.0.0")
    assert not complete.find_all("p", cls="tree__note")


async def test_the_file_page_of_a_huge_manifest_always_includes_the_current_file(
    site, showcase, monkeypatch
):
    monkeypatch.setattr(public, "TREE_LIMIT", 3)

    page = await visit(site, "/registry/skills/pdf-tools/versions/1.1.0/files/scripts/run.py")

    tree = page.find("aside", cls="filepage__tree")
    names = [a.words() for a in tree.find("ul", cls="tree").find_all("a")]
    assert "run.py" in names and len(names) == 4
    assert tree.find("li", cls="is-current").find("a").words() == "run.py"
    assert tree.find("p", cls="tree__note").words() == "Showing 4 of 12 files. Full manifest (JSON)"


def test_limit_files_keeps_the_first_entries_and_the_current_one():
    def entry(path: str) -> ManifestEntryResponse:
        return ManifestEntryResponse(
            path=path, size=1, sha256="0" * 64, media_type="text/plain", text=True
        )

    files = [entry(f"f{i:02d}.txt") for i in range(10)]

    assert [e.path for e in public.limit_files(files, None)] == [e.path for e in files]
    assert public.TREE_LIMIT == 300
    original = public.TREE_LIMIT
    try:
        public.TREE_LIMIT = 4
        assert [e.path for e in public.limit_files(files, None)] == [
            "f00.txt",
            "f01.txt",
            "f02.txt",
            "f03.txt",
        ]
        assert [e.path for e in public.limit_files(files, "f02.txt")] == [
            "f00.txt",
            "f01.txt",
            "f02.txt",
            "f03.txt",
        ]
        assert [e.path for e in public.limit_files(files, "f08.txt")][-1] == "f08.txt"
        assert len(public.limit_files(files, "missing.txt")) == 4
    finally:
        public.TREE_LIMIT = original


async def test_the_registry_offers_a_way_to_skip_the_filters(site, library):
    page = await visit(site, "/registry")

    skip = page.find("aside", cls="facets").find("a", cls="skip-link")
    assert skip.attrs["href"] == "#results" and skip.words() == "Skip filters"
    results = page.by_id("results")
    assert results.attrs["tabindex"] == "-1"
    assert results.attrs["aria-labelledby"] == "results-title"


async def test_the_filters_are_a_disclosure_that_is_open_without_javascript(site, library):
    plain = await visit(site, "/registry")
    filtered = await visit(site, "/registry?kind=skill&tag=docs&q=pdf")

    details = plain.find("details", cls="filters")
    assert "open" in details.attrs and "data-collapse-narrow" in details.attrs
    assert details.find("summary").words() == "Filters"
    assert [h.words() for h in details.find_all("h2")] == ["Kind", "Category", "Tags"]
    assert filtered.find("details", cls="filters").find("summary").words() == "Filters 3 active"


async def test_the_header_shows_the_latest_marker_only_on_the_latest_version(site, showcase):
    latest = await visit(site, "/registry/skills/pdf-tools")
    older = await visit(site, "/registry/skills/pdf-tools?version=1.0.0")
    deprecated = await visit(site, "/registry/skills/pdf-tools?version=0.9.0")

    assert latest.find("p", cls="detail__ids").find("span", cls="tag-latest").words() == "latest"
    assert not older.find("p", cls="detail__ids").find_all("span", cls="tag-latest")
    assert not deprecated.find("p", cls="detail__ids").find_all("span", cls="tag-latest")


async def test_the_page_describes_the_chosen_version_with_its_own_metadata(
    site, db_session, blob_store, actors
):
    owner = actors.owner
    await publish(
        db_session,
        blob_store,
        owner,
        "evolving",
        "1.0.0",
        display_name="Old Name",
        description="Old description.",
        tags=["old"],
        category="legacy",
    )
    await publish(
        db_session,
        blob_store,
        owner,
        "evolving",
        "1.1.0",
        display_name="New Name",
        description="New description.",
        tags=["new"],
        category="modern",
    )

    latest = await visit(site, "/registry/skills/evolving")
    older = await visit(site, "/registry/skills/evolving?version=1.0.0")
    listing = await visit(site, "/registry")

    assert latest.find("h1").words() == "New Name"
    assert latest.find("p", cls="detail__description").words() == "New description."
    assert [a.words() for a in latest.find("ul", cls="chips").find_all("a")] == ["modern", "new"]
    assert older.find("h1").words() == "Old Name"
    assert older.find("title").words() == f"Old Name 1.0.0 {MIDDLE_DOT} Alexandria"
    assert older.find("p", cls="detail__description").words() == "Old description."
    assert older.find("meta", name="description").attrs["content"] == "Old description."
    assert [a.words() for a in older.find("ul", cls="chips").find_all("a")] == ["legacy", "old"]
    assert by_name(listing)["evolving"].find("a", cls="card__link").words() == "New Name"


async def test_hostile_values_stored_directly_in_the_database_are_escaped_in_detail_and_file_pages(
    site, db_session, blob_store, actors
):
    await publish(db_session, blob_store, actors.owner, "tampered", tags=["fine"], category="fine")
    version = await VersionService(db_session).get(None, "tampered")
    listing = await PackageService(db_session, blob_store).manifest(None, "tampered", "1.0.0")
    tags = ["<script>alert(1)</script>", 'x"onmouseover="alert(2)']
    category = '"><svg onload=alert(3)>'
    manifest = [
        {**entry.model_dump(), "media_type": "<script>alert(4)</script>"} for entry in listing.files
    ]
    await db_session.execute(
        update(SkillVersion)
        .where(SkillVersion.id == version.id)
        .values(
            tags=json.dumps(tags),
            category=category,
            display_name="<img src=x onerror=alert(5)>",
            description="<script>alert(6)</script>",
            manifest=json.dumps(manifest),
            metadata_=json.dumps({"<i>key</i>": ["<b>value</b>", {"<u>": 1}]}),
        )
    )
    await db_session.commit()

    page = await visit(site, "/registry/skills/tampered")
    file_page = await visit(site, "/registry/skills/tampered/versions/1.0.0/files/SKILL.md")

    assert page.find("h1").text() == "<img src=x onerror=alert(5)>"
    assert page.find("p", cls="detail__description").text() == "<script>alert(6)</script>"
    chips = page.find("ul", cls="chips").find_all("a")
    assert [a.text() for a in chips] == [category, *tags]
    assert [unquote_plus(a.attrs["href"].split("=", 1)[1]) for a in chips] == [category, *tags]
    shown = {
        d.find("dt").text(): d.find("dd").text()
        for d in page.find("dl", cls="meta").find_all("div")
    }
    assert shown == {"<i>key</i>": '["<b>value</b>", {"<u>": 1}]'}
    assert facts(file_page)["Type"] == "<script>alert(4)</script>"
    assert "<script>alert" not in page.response.text + file_page.response.text


@pytest.mark.parametrize(
    ("source", "targets"),
    [
        ("[<http:>]()", []),
        ("[<https://a.example>](https://b.example)", ["https://b.example"]),
        ("[<https://a.example>]()", ["https://a.example"]),
        ("[[x](https://a.example)](https://b.example)", ["https://a.example"]),
        ("[a](docs/guide.md)[b](nothing.md)[c](https://c.example)", None),
    ],
)
def test_links_inside_links_never_unbalance_the_markup(source, targets):
    fragment = md(source, "docs/guide.md")

    anchors = fragment.find_all("a")
    if targets is not None:
        assert [a.attrs["href"] for a in anchors] == targets
    assert [a.words() for a in anchors if a.find_all("a")] == []


async def test_the_application_serves_the_site_with_its_assets_and_headers(
    client, db_session, blob_store, actors
):
    await publish(
        db_session, blob_store, actors.owner, "wired-entry", description="Reachable through the app"
    )

    landing = await visit(client, "/")
    registry = await visit(client, "/registry?q=wired")
    detail = await visit(client, "/registry/skills/wired-entry")
    file_page = await visit(client, "/registry/skills/wired-entry/versions/1.0.0/files/SKILL.md")
    missing = await visit(client, "/registry/skills/ghost-entry", 404)

    assert [d.attrs["href"] for d in registry.find_all("a", cls="card__link")] == [
        "/registry/skills/wired-entry"
    ]
    assert detail.find("h1").words() == "Wired Entry"
    assert file_page.by_id("file-content").text().startswith("---\nname: wired-entry")
    assert missing.find("h1").words() == "Not found"
    for page in (landing, detail):
        stylesheet = page.find("link", rel="stylesheet").attrs["href"]
        assert (await client.get(stylesheet)).status_code == 200
        assert (await client.get(page.find("script").attrs["src"])).status_code == 200


async def test_the_application_keeps_the_rest_api_and_the_site_apart(client):
    health = await client.get("/api/v1/health")
    page = await visit(client, "/registry")

    assert health.status_code == 200
    assert "content-security-policy" not in health.headers
    assert page.response.headers["content-security-policy"] == CSP
    assert (await client.get("/docs")).status_code == 200
    assert "content-security-policy" not in (await client.get("/docs")).headers


async def test_the_application_answers_401_on_the_site_of_a_private_registry(
    client, db_session, blob_store, actors, monkeypatch
):
    await publish(db_session, blob_store, actors.owner, "wired-entry")
    monkeypatch.setattr(settings.registry, "public_read", False)

    for url in ("/registry", "/registry/skills/wired-entry"):
        page = await visit(client, url, 401)
        assert "wired-entry" not in page.response.text
    assert (await visit(client, "/")).find("p", cls="notice--lock")


def local(url: str) -> str:
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}" == PUBLIC_URL
    return parts.path + (f"?{parts.query}" if parts.query else "")


async def test_the_rest_examples_of_the_landing_page_work_against_the_real_api(
    client, db_session, blob_store, actors
):
    await publish(db_session, blob_store, actors.owner, "pdf-tools", tags=["pdf"])

    landing = await visit(client, "/")

    search, entrypoint, download = (
        shlex.split(landing.by_id(name).text())[-1]
        for name in ("rest-search", "rest-entrypoint", "rest-download")
    )
    found = await client.get(local(search))
    assert found.status_code == 200 and [i["name"] for i in found.json()["items"]] == ["pdf-tools"]
    content = await client.get(local(entrypoint))
    assert content.status_code == 200 and content.json()["name"] == "pdf-tools"
    archive = await client.get(local(download))
    assert archive.status_code == 200 and archive.headers["content-type"] == "application/zip"
    assert zipfile.is_zipfile(io.BytesIO(archive.content))


async def test_the_links_and_commands_of_a_detail_page_work_against_the_real_api(
    client, db_session, blob_store, actors, monkeypatch
):
    await publish(
        db_session,
        blob_store,
        actors.owner,
        "pdf-tools",
        files={
            "scripts/run.py": "print('run')\n",
            "assets/logo.png": b"\x89PNG\r\n" + bytes(40),
        },
    )
    monkeypatch.setattr(public, "TREE_LIMIT", 1)

    page = await visit(client, "/registry/skills/pdf-tools")

    command = page.by_id("use-curl").text().splitlines()
    url = shlex.split(command[0])[-1]
    archive = await client.get(local(url))
    assert archive.status_code == 200
    checksum = shlex.split(command[1])[1].split("  ")[0]
    assert hashlib.sha256(archive.content).hexdigest() == checksum
    assert archive.headers["x-content-sha256"] == checksum
    row = page.find("tbody").find("tr")
    assert row.find("code").attrs["title"] == checksum
    table_link = await client.get(row.find("a", cls="download").attrs["href"])
    assert table_link.content == archive.content
    button = await client.get(page.find("p", cls="panel__foot").find("a").attrs["href"])
    assert button.content == archive.content
    manifest = await client.get(page.find("p", cls="tree__note").find("a").attrs["href"])
    assert manifest.status_code == 200
    assert sorted(item["path"] for item in manifest.json()["files"]) == sorted(
        ["SKILL.md", "assets/logo.png", "scripts/run.py"]
    )


async def test_the_raw_links_of_the_file_pages_work_against_the_real_api(
    client, db_session, blob_store, actors
):
    await publish(
        db_session,
        blob_store,
        actors.owner,
        "pdf-tools",
        files={
            "scripts/run.py": "print('run')\n",
            "assets/logo.png": b"\x89PNG\r\n" + bytes(40),
        },
    )

    text_page = await visit(
        client, "/registry/skills/pdf-tools/versions/1.0.0/files/scripts/run.py"
    )
    binary_page = await visit(
        client, "/registry/skills/pdf-tools/versions/1.0.0/files/assets/logo.png"
    )

    text = await client.get(text_page.find("a", cls="button").attrs["href"])
    assert text.status_code == 200 and text.text == "print('run')\n"
    assert text.headers["content-type"].startswith("text/plain")
    data = await client.get(binary_page.find("a", cls="button").attrs["href"])
    assert data.status_code == 200 and data.content == b"\x89PNG\r\n" + bytes(40)
    assert data.headers["content-type"] == "application/octet-stream"
    assert data.headers["content-disposition"].startswith("attachment")


async def test_the_title_of_a_result_page_says_which_page_it_is(site, bulk):
    first = await visit(site, "/registry")
    second = await visit(site, "/registry?q=bulk&page=2")

    assert first.find("title").words() == f"Registry {MIDDLE_DOT} Alexandria"
    assert second.find("title").words() == (
        f"bulk {MIDDLE_DOT} Registry {MIDDLE_DOT} Page 2 {MIDDLE_DOT} Alexandria"
    )


def test_the_markdown_rule_tables_are_compiled_before_any_thread_renders():
    markdown = public.build_markdown()

    for ruler in (
        markdown.core.ruler,
        markdown.block.ruler,
        markdown.inline.ruler,
        markdown.inline.ruler2,
    ):
        assert ruler.__cache__ is not None


def test_rendering_from_many_threads_gives_the_same_output_as_rendering_alone():
    context = public.MarkdownContext("pkg", "1.0.0", frozenset({"docs/guide.md"}))
    documents = [
        "# Title\n\n| a | b |\n|:-|-:|\n| 1 | 2 |\n\n[g](docs/guide.md) ![i](x.png) ~~s~~",
        "## Two\n\n> quote\n\n```py\nprint(1)\n```\n\n1. one\n2. two\n\n[x](javascript:alert(1))",
        "Setext\n======\n\n[a](#setext) <https://e.org> `code` **bold** *em*",
        "<script>alert(1)</script>\n\n- [ ] item\n- [x] done\n",
    ]
    expected = [public.render_html(document, context) for document in documents]

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(lambda document: public.render_html(document, context), documents * 25)
        )

    assert results == expected * 25


PLAIN_CONTEXT = public.MarkdownContext("pkg", "1.0.0", frozenset())

FLOODS = {
    "empty-headings": "#\n" * 32000,
    "equal-headings": "# h\n" * 16000,
    "setext-headings": "a\n=\n" * 16000,
    "heading-links": "# [a](b)\n" * 7000,
    "heading-bold": "# **a**\n" * 8000,
    "heading-code": "# `a`\n" * 9000,
}


def reference_anchor(base: str, anchors: set[str]) -> str:
    candidate, number = base, 0
    while candidate in anchors:
        number += 1
        candidate = f"{base}-{number}"
    anchors.add(candidate)
    return candidate


class CountingSet(set):
    def __init__(self) -> None:
        super().__init__()
        self.checks = 0

    def __contains__(self, item: object) -> bool:
        self.checks += 1
        return super().__contains__(item)


def children_of(fragment: Node) -> list[str]:
    return [child.tag for child in fragment.children if isinstance(child, Node)]


def test_anchors_are_still_the_smallest_free_suffix():
    generator = random.Random(2027)
    pool = ["a", "b", "a-1", "a-2", "a-3", "b-1", "a-1-1", "section", "section-1", "x"]
    for _ in range(300):
        bases = [generator.choice(pool) for _ in range(generator.randint(1, 60))]
        anchors: set[str] = set()
        counters: dict[str, int] = {}
        oracle: set[str] = set()

        got = [public.unique_anchor(base, anchors, counters) for base in bases]

        assert got == [reference_anchor(base, oracle) for base in bases]
        assert anchors == oracle


def test_equal_headings_cost_a_constant_number_of_lookups_each():
    anchors = CountingSet()
    counters: dict[str, int] = {}
    total = 20000

    results = [public.unique_anchor("same", anchors, counters) for _ in range(total)]

    assert results[:3] == ["same", "same-1", "same-2"]
    assert results[-1] == f"same-{total - 1}"
    assert len(set(results)) == total
    assert anchors.checks <= 2 * total


def test_headings_that_collide_with_generated_anchors_keep_distinct_ids():
    fragment = md("# a\n\n# a-1\n\n# a\n\n# a\n\n# a-1\n\n# a-1-1\n")

    identifiers = [h.attrs["id"] for h in fragment.find_all() if re.fullmatch(r"h\d", h.tag)]
    assert identifiers == ["md-a", "md-a-1", "md-a-2", "md-a-3", "md-a-1-1", "md-a-1-1-1"]
    assert len(set(identifiers)) == len(identifiers)


@pytest.mark.parametrize("source", FLOODS.values(), ids=FLOODS)
def test_heading_floods_render_in_a_fraction_of_a_second_as_plain_text(source):
    started = time.perf_counter()
    html = public.render_html(source, PLAIN_CONTEXT)
    elapsed = time.perf_counter() - started

    fragment = parse(html)
    assert elapsed < 0.5
    assert len(html) < len(source) + 1000
    assert fragment.problems == []
    assert children_of(fragment) == ["p", "pre"]
    assert fragment.find("p").words() == public.PLAIN_NOTICE
    assert fragment.find("pre").text() == source


def test_a_document_with_exactly_the_heading_limit_is_still_formatted():
    source = "".join(
        f"# Title {index}\n\ntext\n\n" for index in range(public.MARKDOWN_HEADING_LIMIT)
    )

    started = time.perf_counter()
    fragment = md(source)

    assert time.perf_counter() - started < 0.5
    assert len(fragment.find_all("h2")) == public.MARKDOWN_HEADING_LIMIT
    assert not fragment.find_all("pre")
    assert PLAIN_TEXT_NOTICE not in fragment.text()


PLAIN_TEXT_NOTICE = public.PLAIN_NOTICE


def test_one_heading_over_the_limit_turns_the_document_into_escaped_plain_text():
    source = "".join(
        f"# <b>Title</b> {index}\n\ntext\n\n" for index in range(public.MARKDOWN_HEADING_LIMIT + 1)
    )

    fragment = md(source)

    assert children_of(fragment) == ["p", "pre"]
    assert not fragment.find_all("h2") and not fragment.find_all("b")
    assert fragment.find("p", cls="notice").words() == PLAIN_TEXT_NOTICE
    assert fragment.find("pre").attrs["tabindex"] == "0"
    assert fragment.find("pre").text() == source


def test_the_line_limit_is_exact_and_counts_every_kind_of_line_break():
    allowed = "line\n" * (public.MARKDOWN_LINE_LIMIT - 1) + "line"

    assert public.line_count("") == 1
    assert public.line_count("a\rb\nc") == 3
    assert public.line_count("a\r\nb\r\n") == 3
    assert public.line_count("a\r\n\r\nb\n\rc") == 5
    assert public.line_count(allowed) == public.MARKDOWN_LINE_LIMIT
    assert public.line_count(allowed + "\n") == public.MARKDOWN_LINE_LIMIT + 1
    formatted = md(allowed)
    plain = md(allowed + "\n")
    assert children_of(formatted) == ["p"] and PLAIN_TEXT_NOTICE not in formatted.text()
    assert children_of(plain) == ["p", "pre"] and PLAIN_TEXT_NOTICE in plain.text()


def test_windows_line_endings_do_not_halve_the_line_limit():
    document = "line\r\n" * (public.MARKDOWN_LINE_LIMIT - 1)

    assert public.line_count(document) == public.MARKDOWN_LINE_LIMIT
    assert "notice" not in public.render_html(document, PLAIN_CONTEXT)
    assert "notice" in public.render_html(document + "more\r\n", PLAIN_CONTEXT)


def test_a_page_of_links_that_would_exceed_the_output_limit_is_shown_as_plain_text():
    context = public.MarkdownContext("n" * 64, "1.0.0-" + "r" * 40, frozenset({"b"}))
    source = "[a](b) " * 9000
    assert len(source) < public.MARKDOWN_LIMIT

    html = public.render_html(source, context)
    small = public.render_html("[a](b) " * 100, context)

    assert len(html) < 2 * len(source)
    assert parse(html).find("p", cls="notice").words() == PLAIN_TEXT_NOTICE
    assert "notice" not in small and small.count("<a href=") == 100
    assert len(small) < public.MARKDOWN_OUTPUT_LIMIT


def test_a_document_over_the_cpu_budget_is_shown_as_plain_text(monkeypatch):
    source = "# Title\n\nSome *text* and a [link](https://example.com).\n"
    assert "notice" not in public.render_html(source, PLAIN_CONTEXT)

    monkeypatch.setattr(public, "MARKDOWN_CPU_BUDGET", -1.0)
    fragment = md(source)

    assert children_of(fragment) == ["p", "pre"]
    assert fragment.find("pre").text() == source


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        ("x\n" * (public.MARKDOWN_LINE_LIMIT + 1), "lines"),
        ("# h\n\n" * (public.MARKDOWN_HEADING_LIMIT + 1), "headings"),
    ],
)
def test_the_reason_for_the_plain_text_fallback_is_logged_once(caplog, source, reason):
    context = public.MarkdownContext("some-pkg", "1.2.3", frozenset())

    with caplog.at_level(logging.INFO, logger="app.api.public"):
        public.render_html(source, context)

    records = [record for record in caplog.records if record.name == "app.api.public"]
    assert len(records) == 1
    assert records[0].extra_data == {"name": "some-pkg", "version": "1.2.3", "reason": reason}


def test_the_cpu_and_output_budgets_are_logged_with_their_own_reason(caplog, monkeypatch):
    context = public.MarkdownContext("some-pkg", "1.2.3", frozenset({"b"}))
    monkeypatch.setattr(public, "MARKDOWN_OUTPUT_LIMIT", 100)

    with caplog.at_level(logging.INFO, logger="app.api.public"):
        public.render_html("[a](b) and some more text to pass the limit " * 5, context)
        monkeypatch.setattr(public, "MARKDOWN_OUTPUT_LIMIT", 10**9)
        monkeypatch.setattr(public, "MARKDOWN_CPU_BUDGET", -1.0)
        public.render_html("# Title\n", context)
        monkeypatch.setattr(public, "MARKDOWN_CPU_BUDGET", 1.0)
        public.render_html("# Title\n", context)

    reasons = [record.extra_data["reason"] for record in caplog.records]
    assert reasons == ["output", "cpu"]


def test_the_inline_rule_creates_the_children_of_an_inline_token_that_has_none():
    state = StateCore("", public.MARKDOWN, public.render_env(PLAIN_CONTEXT))
    token = Token("inline", "", 0)
    token.content = "*a*"
    token.children = None
    state.tokens.append(token)

    public.inline_rule(state)

    assert [child.type for child in token.children] == ["em_open", "text", "em_close"]


def test_the_cpu_budget_is_checked_while_the_inline_content_is_parsed(monkeypatch):
    calls = []
    original = public.spend

    def counting(state):
        calls.append(1)
        original(state)

    monkeypatch.setattr(public, "spend", counting)

    public.render_html("one\n\ntwo\n\nthree\n", PLAIN_CONTEXT)

    assert len(calls) >= 3


def test_plain_text_reveals_bidirectional_controls_and_keeps_markup_inert():
    hostile = f"evil {chr(0x202E)} <script>alert(1)</script> &amp; <img src=x onerror=alert(1)>\n"
    source = "# a\n" * (public.MARKDOWN_HEADING_LIMIT + 1) + hostile

    fragment = md(source)

    shown = fragment.find("pre").text()
    assert chr(0x202E) not in shown and "<U+202E>" in shown
    assert "<script>alert(1)</script> &amp; <img src=x onerror=alert(1)>" in shown
    assert not fragment.find_all("script") and not fragment.find_all("img")


def test_an_empty_document_is_not_mistaken_for_a_failed_render():
    assert public.render_html("", PLAIN_CONTEXT) == ""
    assert public.render_html("   \n\n", PLAIN_CONTEXT) == ""


async def test_the_plain_text_fallback_is_cached_like_any_other_result(monkeypatch):
    calls = []
    original = public.render_html

    def spy(text, context):
        calls.append(threading.current_thread().name)
        return original(text, context)

    monkeypatch.setattr(public, "render_html", spy)
    source = "#\n" * 20000

    first = await public.render_markdown(source, PLAIN_CONTEXT)
    second = await public.render_markdown(source, PLAIN_CONTEXT)

    assert first is second
    assert PLAIN_TEXT_NOTICE in first
    assert len(calls) == 1


class Gate:
    def __init__(self) -> None:
        self.open = threading.Event()
        self.entered = threading.Event()
        self.calls: list[str] = []

    def wrap(self, original):
        def gated(text, context):
            self.calls.append(threading.current_thread().name)
            self.entered.set()
            assert self.open.wait(10)
            return original(text, context)

        return gated


async def wait_until(condition, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "the condition never became true"
        await asyncio.sleep(0.005)


async def test_concurrent_requests_for_one_document_share_a_single_render(monkeypatch):
    original = public.render_html
    gate = Gate()
    monkeypatch.setattr(public, "render_html", gate.wrap(original))

    tasks = [
        asyncio.create_task(public.render_markdown("# Same", PLAIN_CONTEXT)) for _ in range(24)
    ]
    await wait_until(gate.entered.is_set)
    await asyncio.sleep(0.05)
    assert len(public.MARKDOWN_PENDING) == 1
    gate.open.set()
    results = await asyncio.gather(*tasks)

    assert len(gate.calls) == 1
    assert all(result is results[0] for result in results)
    assert results[0] == original("# Same", PLAIN_CONTEXT)
    assert public.MARKDOWN_PENDING == {}
    assert list(public.MARKDOWN_CACHE.values()) == [results[0]]


async def test_different_documents_are_not_merged(monkeypatch):
    gate = Gate()
    gate.open.set()
    monkeypatch.setattr(public, "render_html", gate.wrap(public.render_html))
    other = public.MarkdownContext("pkg", "2.0.0", frozenset())

    results = await asyncio.gather(
        public.render_markdown("# Same", PLAIN_CONTEXT),
        public.render_markdown("# Same", other),
        public.render_markdown("# Different", PLAIN_CONTEXT),
    )

    assert len(gate.calls) == 3
    assert len({id(result) for result in results}) == 3


async def test_a_waiter_that_goes_away_does_not_cancel_the_shared_render(monkeypatch):
    original = public.render_html
    gate = Gate()
    monkeypatch.setattr(public, "render_html", gate.wrap(original))
    first = asyncio.create_task(public.render_markdown("# Shared", PLAIN_CONTEXT))
    second = asyncio.create_task(public.render_markdown("# Shared", PLAIN_CONTEXT))
    await wait_until(gate.entered.is_set)
    await asyncio.sleep(0.02)

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    gate.open.set()
    result = await second

    assert result == original("# Shared", PLAIN_CONTEXT)
    assert len(gate.calls) == 1
    assert public.MARKDOWN_PENDING == {}
    assert public.fingerprint("# Shared", PLAIN_CONTEXT) in public.MARKDOWN_CACHE


async def test_a_render_nobody_waits_for_any_more_still_fills_the_cache(monkeypatch):
    gate = Gate()
    monkeypatch.setattr(public, "render_html", gate.wrap(public.render_html))
    only = asyncio.create_task(public.render_markdown("# Orphan", PLAIN_CONTEXT))
    await wait_until(gate.entered.is_set)

    only.cancel()
    with pytest.raises(asyncio.CancelledError):
        await only
    gate.open.set()
    await wait_until(lambda: not public.MARKDOWN_PENDING)

    assert public.fingerprint("# Orphan", PLAIN_CONTEXT) in public.MARKDOWN_CACHE
    assert len(gate.calls) == 1


async def test_a_failing_render_reaches_every_waiter_and_is_not_remembered(monkeypatch):
    original = public.render_html

    def broken(text, context):
        time.sleep(0.05)
        raise RuntimeError("renderer bug")

    monkeypatch.setattr(public, "render_html", broken)
    outcomes = await asyncio.gather(
        *[public.render_markdown("# Broken", PLAIN_CONTEXT) for _ in range(5)],
        return_exceptions=True,
    )

    assert [type(outcome) for outcome in outcomes] == [RuntimeError] * 5
    assert public.MARKDOWN_PENDING == {} and not public.MARKDOWN_CACHE
    monkeypatch.setattr(public, "render_html", original)
    assert await public.render_markdown("# Broken", PLAIN_CONTEXT) == original(
        "# Broken", PLAIN_CONTEXT
    )


async def test_a_pending_render_of_another_event_loop_is_never_awaited():
    key = public.fingerprint("# Stale", PLAIN_CONTEXT)
    elsewhere = asyncio.new_event_loop()
    try:
        public.MARKDOWN_PENDING[key] = elsewhere.create_future()

        result = await asyncio.wait_for(public.render_markdown("# Stale", PLAIN_CONTEXT), 5)

        assert result == public.render_html("# Stale", PLAIN_CONTEXT)
        assert public.MARKDOWN_PENDING == {}
    finally:
        elsewhere.close()


async def test_renders_run_one_at_a_time_on_a_dedicated_thread(monkeypatch):
    lock = threading.Lock()
    names: set[str] = set()
    running = 0
    peak = 0
    original = public.render_html

    def watched(text, context):
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
            names.add(threading.current_thread().name)
        time.sleep(0.05)
        with lock:
            running -= 1
        return original(text, context)

    monkeypatch.setattr(public, "render_html", watched)

    await asyncio.gather(
        *[public.render_markdown(f"# Document {index}", PLAIN_CONTEXT) for index in range(12)]
    )

    assert public.MARKDOWN_WORKERS == 1
    assert peak == public.MARKDOWN_WORKERS
    assert names and all(name.startswith("markdown") for name in names)


def test_the_thread_switch_interval_is_lowered_only_while_a_render_is_active():
    before = sys.getswitchinterval()
    slices = public.ShortTimeslices()

    with slices:
        assert sys.getswitchinterval() == pytest.approx(public.MARKDOWN_TIMESLICE)
        with slices:
            assert sys.getswitchinterval() == pytest.approx(public.MARKDOWN_TIMESLICE)
        assert sys.getswitchinterval() == pytest.approx(public.MARKDOWN_TIMESLICE)
    assert sys.getswitchinterval() == before
    with pytest.raises(RuntimeError, match="boom"), slices:
        raise RuntimeError("boom")
    assert sys.getswitchinterval() == before
    assert slices.active == 0


async def test_the_switch_interval_follows_the_renders_in_flight_and_survives_failures(
    monkeypatch,
):
    before = sys.getswitchinterval()
    original = public.render_html
    gate = Gate()
    monkeypatch.setattr(public, "render_html", gate.wrap(original))
    first = asyncio.create_task(public.render_markdown("# One", PLAIN_CONTEXT))
    second = asyncio.create_task(public.render_markdown("# Two", PLAIN_CONTEXT))
    try:
        await wait_until(gate.entered.is_set)
        await asyncio.sleep(0.02)
        assert sys.getswitchinterval() == pytest.approx(public.MARKDOWN_TIMESLICE)
        first.cancel()
        await asyncio.sleep(0.02)
        assert sys.getswitchinterval() == pytest.approx(public.MARKDOWN_TIMESLICE)
    finally:
        gate.open.set()
    await asyncio.gather(first, second, return_exceptions=True)
    await wait_until(lambda: not public.MARKDOWN_PENDING)
    assert sys.getswitchinterval() == before

    def broken(text, context):
        raise RuntimeError("renderer bug")

    monkeypatch.setattr(public, "render_html", broken)
    with pytest.raises(RuntimeError, match="renderer bug"):
        await public.render_markdown("# Three", PLAIN_CONTEXT)
    assert sys.getswitchinterval() == before


async def test_the_detail_page_of_a_flooded_entry_point_is_plain_text_and_quick(
    site, db_session, blob_store, actors
):
    await publish(db_session, blob_store, actors.owner, "flooded", body="#\n" * 32000)

    started = time.perf_counter()
    page = await visit(site, "/registry/skills/flooded")
    elapsed = time.perf_counter() - started

    prose = page.find("div", cls="prose")
    assert prose.find("p", cls="notice").words() == PLAIN_TEXT_NOTICE
    assert prose.find("pre").text().lstrip("\n") == "#\n" * 32000
    assert not prose.find_all("h2")
    assert elapsed < 2.0
