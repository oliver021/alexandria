from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import shlex
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote, unquote, urlencode

import jinja2
from fastapi import APIRouter, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, Response
from fastapi.routing import APIRoute
from markdown_it import MarkdownIt
from markdown_it.renderer import RendererHTML
from markdown_it.rules_core import StateCore
from markdown_it.token import Token
from markupsafe import Markup
from starlette.templating import Jinja2Templates

from app.api.deps import DbSession, Store, release_connection
from app.config.settings import settings
from app.logging import get_logger
from app.models.schemas import ManifestEntryResponse, TaxonomyItem
from app.services import mappers
from app.services.errors import (
    DomainError,
    ForbiddenError,
    InvalidPackageError,
    NotFoundError,
    UnauthorizedError,
    ValidationError,
)
from app.services.package_inspector import split_frontmatter
from app.services.package_service import PackageService
from app.services.skill_service import SkillService
from app.services.version_service import VersionService

logger = get_logger("app.api.public")

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = ROOT / "web" / "templates"
STATIC_DIR = ROOT / "web" / "static"
STATIC_URL = "/static"

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self'; img-src 'self' data:; script-src 'self'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}

KINDS = ("skill", "grounding")
KIND_LABELS = {"skill": "Skill", "grounding": "Grounding"}
SORT_LABELS = {
    "relevance": "Relevance",
    "name": "Name",
    "updated": "Recently updated",
    "created": "Newest",
}
PAGE_SIZE = 20
MAX_PAGE = 10_000
MAX_TAG_FILTERS = 20
MAX_QUERY_STRING = 8192
TAG_FACETS = 24
CATEGORY_FACETS = 12
RECENT_ENTRIES = 6
TREE_DEPTH = 6
TREE_LIMIT = 300
MAX_METADATA_ITEMS = 24
MAX_METADATA_LENGTH = 300
MARKDOWN_LIMIT = 64 * 1024
MARKDOWN_CACHE_SIZE = 64
MARKDOWN_LINE_LIMIT = 10_000
MARKDOWN_HEADING_LIMIT = 500
MARKDOWN_OUTPUT_LIMIT = 1024 * 1024
MARKDOWN_CPU_BUDGET = 1.0
MARKDOWN_WORKERS = 1
MARKDOWN_TIMESLICE = 0.0005
PLAIN_NOTICE = (
    "This entry point is too large or too complex to format, so it is shown as plain text."
)
HEADING_SHIFT = 1
ANCHOR_PREFIX = "md-"
PLACEHOLDER_NAME = "my-skill"
EXTERNAL_SCHEMES = frozenset({"http", "https", "mailto"})

ERROR_STATUS: tuple[tuple[type[DomainError], int], ...] = (
    (UnauthorizedError, 401),
    (ForbiddenError, 403),
    (NotFoundError, 404),
    (ValidationError, 400),
)
ERROR_TITLES = {
    400: "Invalid request",
    401: "This registry is private",
    403: "Not allowed",
    404: "Not found",
    500: "Something went wrong",
}
ERROR_FIXED_TEXT = {
    401: (
        "Reading this registry requires a credential, and this website does not sign anyone "
        "in. Use the REST API or the MCP endpoint with an Authorization: Bearer header."
    ),
    403: "You are not allowed to see this page.",
    500: "The server could not complete the request. Try again later.",
}


def status_of(exc: DomainError) -> int:
    for kind, status in ERROR_STATUS:
        if isinstance(exc, kind):
            return 404 if exc.reason == "INVALID_PATH" else status
    return 500


SIZE_UNITS = ("KiB", "MiB", "GiB")


def human_size(value: int) -> str:
    size = float(value)
    if size < 1024:
        return f"{int(size)} B"
    unit = SIZE_UNITS[0]
    for unit in SIZE_UNITS:
        size /= 1024
        if size < 1024:
            break
    return f"{size:.1f} {unit}" if size < 10 else f"{size:.0f} {unit}"


def short_sha(value: str) -> str:
    return value[:12]


def iso_date(value: datetime | None) -> str:
    return value.astimezone(UTC).date().isoformat() if value else "-"


def plural(count: int, singular: str, many: str) -> str:
    return f"{count} {singular if count == 1 else many}"


def skill_url(name: str) -> str:
    return f"/registry/skills/{quote(name, safe='')}"


def version_url(name: str, version: str) -> str:
    return f"{skill_url(name)}?{urlencode({'version': version})}"


def file_url(name: str, version: str, path: str) -> str:
    return f"{skill_url(name)}/versions/{quote(version, safe='')}/files/{quote(path, safe='/')}"


def manifest_url(name: str, version: str) -> str:
    return (
        f"{mappers.API_PREFIX}/skills/{quote(name, safe='')}"
        f"/versions/{quote(version, safe='')}/files"
    )


def raw_url(name: str, version: str, path: str) -> str:
    return (
        f"{mappers.API_PREFIX}/skills/{quote(name, safe='')}"
        f"/versions/{quote(version, safe='')}/raw/{quote(path, safe='/')}"
    )


def download_path(name: str, version: str) -> str:
    return mappers.download_url("", name, version)


def asset_url(name: str) -> str:
    try:
        stamp = (STATIC_DIR / name).stat().st_mtime_ns // 1_000_000
    except OSError:
        return f"{STATIC_URL}/{name}"
    return f"{STATIC_URL}/{name}?v={stamp:x}"


def build_templates() -> Jinja2Templates:
    environment = jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
        autoescape=True,
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    environment.filters.update(
        human_size=human_size, short_sha=short_sha, iso_date=iso_date, plural=plural
    )
    environment.globals.update(
        asset=asset_url,
        skill_url=skill_url,
        version_url=version_url,
        file_url=file_url,
        raw_url=raw_url,
        manifest_url=manifest_url,
        download_path=download_path,
        kind_label=lambda kind: KIND_LABELS.get(kind, kind),
        sort_label=lambda sort: SORT_LABELS.get(sort, sort),
    )
    return Jinja2Templates(env=environment)


templates = build_templates()


def render(request: Request, name: str, context: dict[str, Any], status: int = 200) -> Response:
    base = {"public_url": settings.server.public_url, "nav": "", "robots": "", "canonical": ""}
    return templates.TemplateResponse(request, name, {**base, **context}, status_code=status)


def error_page(request: Request, exc: DomainError | RequestValidationError) -> Response:
    if isinstance(exc, RequestValidationError):
        status = 400
        detail = "; ".join(
            f"{'.'.join(str(part) for part in item['loc'][1:])}: {item['msg']}"
            for item in exc.errors()
        )
    else:
        status = status_of(exc)
        detail = exc.message
    shown = ERROR_FIXED_TEXT.get(status, detail)
    response = render(
        request,
        "error.html",
        {
            "status": status,
            "title": ERROR_TITLES[status],
            "detail": shown,
            "robots": "noindex",
        },
        status,
    )
    if status == 401:
        response.headers["WWW-Authenticate"] = "Bearer"
    return response


class SiteRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        handler = super().get_route_handler()

        async def site_handler(request: Request) -> Response:
            try:
                if len(request.scope.get("query_string", b"")) > MAX_QUERY_STRING:
                    raise ValidationError(
                        f"The query string must have at most {MAX_QUERY_STRING} characters"
                    )
                response = await handler(request)
            except (DomainError, RequestValidationError) as exc:
                response = error_page(request, exc)
            response.headers.update(SECURITY_HEADERS)
            return response

        return site_handler


router = APIRouter(route_class=SiteRoute)


def route(path: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    return router.api_route(
        path, methods=["GET", "HEAD"], response_class=HTMLResponse, include_in_schema=False
    )


def normalized(value: str | None) -> str:
    return (value or "").strip().lower()


@dataclass(frozen=True)
class Filters:
    q: str = ""
    kind: str = ""
    category: str = ""
    sort: str = ""
    tags: tuple[str, ...] = ()
    page: int = 1

    @classmethod
    def parse(
        cls,
        q: str | None,
        kind: str | None,
        category: str | None,
        sort: str | None,
        tags: Sequence[str] | None,
        page: int,
    ) -> Filters:
        unique = dict.fromkeys(normalized(tag) for tag in tags or ())
        return cls(
            q=(q or "").strip(),
            kind=normalized(kind),
            category=normalized(category),
            sort=normalized(sort),
            tags=tuple(tag for tag in unique if tag),
            page=page,
        )

    @property
    def active(self) -> bool:
        return bool(self.q or self.kind or self.category or self.tags)

    def url(self, **changes: Any) -> str:
        state = replace(self, **changes)
        pairs: list[tuple[str, str]] = []
        if state.q:
            pairs.append(("q", state.q))
        if state.kind:
            pairs.append(("kind", state.kind))
        if state.category:
            pairs.append(("category", state.category))
        pairs.extend(("tag", tag) for tag in state.tags)
        if state.sort:
            pairs.append(("sort", state.sort))
        if state.page > 1:
            pairs.append(("page", str(state.page)))
        return "/registry" + (f"?{urlencode(pairs)}" if pairs else "")

    def chips(self) -> list[dict[str, str]]:
        chips = []
        if self.q:
            chips.append({"label": f"Search: {self.q}", "url": self.url(q="", page=1)})
        if self.kind:
            chips.append({"label": f"Kind: {self.kind}", "url": self.url(kind="", page=1)})
        if self.category:
            chips.append(
                {"label": f"Category: {self.category}", "url": self.url(category="", page=1)}
            )
        for tag in self.tags:
            remaining = tuple(item for item in self.tags if item != tag)
            chips.append({"label": f"Tag: {tag}", "url": self.url(tags=remaining, page=1)})
        return chips


def tag_facets(items: Sequence[TaxonomyItem], filters: Filters) -> list[dict[str, Any]]:
    facets: list[dict[str, Any]] = []
    listed = {item.name for item in items}
    for tag in filters.tags:
        if tag not in listed:
            remaining = tuple(item for item in filters.tags if item != tag)
            facets.append(
                {
                    "name": tag,
                    "count": None,
                    "active": True,
                    "url": filters.url(tags=remaining, page=1),
                }
            )
    for item in items:
        active = item.name in filters.tags
        tags = (
            tuple(tag for tag in filters.tags if tag != item.name)
            if active
            else (*filters.tags, item.name)
        )
        facets.append(
            {
                "name": item.name,
                "count": item.count,
                "active": active,
                "url": filters.url(tags=tags, page=1),
            }
        )
    return facets


def category_facets(items: Sequence[TaxonomyItem], filters: Filters) -> list[dict[str, Any]]:
    facets: list[dict[str, Any]] = []
    listed = {item.name for item in items}
    if filters.category and filters.category not in listed:
        facets.append(
            {
                "name": filters.category,
                "count": None,
                "active": True,
                "url": filters.url(category="", page=1),
            }
        )
    for item in items:
        active = item.name == filters.category
        facets.append(
            {
                "name": item.name,
                "count": item.count,
                "active": active,
                "url": filters.url(category="" if active else item.name, page=1),
            }
        )
    return facets


def page_numbers(current: int, pages: int) -> list[int | None]:
    wanted = {1, pages, *range(current - 2, current + 3)}
    window: list[int | None] = []
    previous = 0
    for number in sorted(n for n in wanted if 1 <= n <= pages):
        if previous and number - previous == 2:
            window.append(previous + 1)
        elif previous and number - previous > 2:
            window.append(None)
        window.append(number)
        previous = number
    return window


def pagination(filters: Filters, total: int) -> dict[str, Any]:
    pages = max(1, math.ceil(total / PAGE_SIZE))
    current = filters.page
    first = (current - 1) * PAGE_SIZE + 1
    return {
        "pages": pages,
        "current": current,
        "first": first,
        "last": min(total, current * PAGE_SIZE),
        "previous": filters.url(page=min(current, pages + 1) - 1) if current > 1 else None,
        "next": filters.url(page=current + 1) if current < pages else None,
        "last_url": filters.url(page=pages),
        "numbers": [
            None if number is None else {"number": number, "url": filters.url(page=number)}
            for number in page_numbers(min(current, pages), pages)
        ],
    }


@dataclass
class TreeNode:
    name: str
    path: str
    is_dir: bool
    size: int = 0
    text: bool = True
    current: bool = False
    entrypoint: bool = False
    open: bool = False
    count: int = 0
    children: list[TreeNode] = field(default_factory=list)


def limit_files(
    files: Sequence[ManifestEntryResponse], current: str | None
) -> list[ManifestEntryResponse]:
    shown = list(files[:TREE_LIMIT])
    if current is not None and all(item.path != current for item in shown):
        shown.extend(item for item in files if item.path == current)
    return shown


def build_tree(
    files: Sequence[ManifestEntryResponse], current: str | None, entrypoint: str
) -> list[TreeNode]:
    root = TreeNode("", "", True)
    index: dict[str, TreeNode] = {}
    for entry in files:
        parts = entry.path.split("/")
        directories, label = parts[:-1], parts[-1]
        if len(directories) > TREE_DEPTH:
            label = "/".join(parts[TREE_DEPTH:])
            directories = directories[:TREE_DEPTH]
        parent = root
        for depth, directory in enumerate(directories, start=1):
            key = "/".join(directories[:depth])
            node = index.get(key)
            if node is None:
                node = TreeNode(directory, key, True)
                index[key] = node
                parent.children.append(node)
            node.count += 1
            parent = node
        parent.children.append(
            TreeNode(
                label,
                entry.path,
                False,
                size=entry.size,
                text=entry.text,
                current=entry.path == current,
                entrypoint=entry.path == entrypoint,
            )
        )
    small = len(files) <= 40
    for node in index.values():
        node.open = (
            small
            or node.path.count("/") == 0
            or bool(current and current.startswith(node.path + "/"))
        )
    return sort_nodes(root.children)


def sort_nodes(nodes: list[TreeNode]) -> list[TreeNode]:
    ordered = sorted(nodes, key=lambda node: (not node.is_dir, node.name.lower(), node.name))
    for node in ordered:
        node.children = sort_nodes(node.children)
    return ordered


BIDI_RANGES = ((0x061C, 0x061C), (0x200E, 0x200F), (0x202A, 0x202E), (0x2066, 0x2069))
BIDI_CONTROLS = re.compile(
    "[" + "".join(f"{re.escape(chr(a))}-{re.escape(chr(b))}" for a, b in BIDI_RANGES) + "]"
)


def reveal_bidi(text: str) -> str:
    return BIDI_CONTROLS.sub(lambda match: f"<U+{ord(match.group()):04X}>", text)


BYTE_ORDER_MARK = chr(0xFEFF)


def strip_frontmatter(text: str) -> str:
    try:
        return split_frontmatter(text.removeprefix(BYTE_ORDER_MARK))[1]
    except InvalidPackageError:
        return text


def clip_markdown(text: str) -> tuple[str, bool]:
    if len(text) <= MARKDOWN_LIMIT:
        return text, False
    cut = text.rfind("\n", MARKDOWN_LIMIT // 2, MARKDOWN_LIMIT)
    return text[: cut if cut > 0 else MARKDOWN_LIMIT], True


@dataclass(frozen=True)
class MarkdownContext:
    name: str
    version: str
    files: frozenset[str]


WARMUP_DOCUMENT = "# a\n\n| b |\n|---|\n| c |\n\n[d](e) *f* ~~g~~ ![h](i)\n"
SCHEME = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):")
EXTERNAL_LINK = re.compile(r"https?://[^/\s?#]+|mailto:\S+", re.IGNORECASE)
SLUG_STRIP = re.compile(r"[^\w\- ]")
ALIGNMENTS = {"text-align:left": "left", "text-align:right": "right", "text-align:center": "center"}


def safe_link(url: str) -> bool:
    text = url.strip()
    match = SCHEME.match(text)
    if match:
        return match.group(1).lower() in EXTERNAL_SCHEMES
    return not text.startswith(("//", "\\", "/\\"))


def slugify(text: str) -> str:
    return SLUG_STRIP.sub("", text.strip().lower()).replace(" ", "-")[:80] or "section"


def resolve_package_path(href: str) -> str | None:
    target = unquote(href.split("#", 1)[0].split("?", 1)[0])
    if not target or target.startswith("/") or "\\" in target or "\x00" in target:
        return None
    parts: list[str] = []
    for segment in target.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(segment)
    return "/".join(parts) or None


def link_attrs(
    href: str | None, context: MarkdownContext, anchors: set[str]
) -> dict[str, str] | None:
    if not href:
        return None
    if href.startswith("#"):
        slug = unquote(href[1:]).lower()
        return {"href": f"#{ANCHOR_PREFIX}{quote(slug)}"} if slug in anchors else None
    if SCHEME.match(href):
        if EXTERNAL_LINK.match(href) is None:
            return None
        return {"href": href, "rel": "nofollow ugc noreferrer"}
    path = resolve_package_path(href)
    if path is None or path not in context.files:
        return None
    return {"href": file_url(context.name, context.version, path)}


def inline_text(token: Token) -> str:
    pieces = token.children or []
    return "".join(piece.content for piece in pieces if piece.type in ("text", "code_inline"))


def label_tokens(label: str) -> list[Token]:
    opening = Token("span_open", "span", 1)
    opening.attrSet("class", "md-image")
    text = Token("text", "", 0)
    text.content = label
    return [opening, text, Token("span_close", "span", -1)]


def rewrite_inline(
    children: list[Token], context: MarkdownContext, anchors: set[str]
) -> list[Token]:
    output: list[Token] = []
    hidden_links: list[bool] = []
    for child in children:
        if child.type == "image":
            alt = inline_text(child).strip()
            output.extend(label_tokens(f"Image: {alt}" if alt else "Image"))
            continue
        if child.type == "link_open":
            inside_link = not all(hidden_links)
            attrs = None if inside_link else link_attrs(child.attrGet("href"), context, anchors)
            hidden_links.append(attrs is None)
            if attrs is None:
                child.hidden = True
            else:
                child.attrs = {**{k: v for k, v in child.attrs.items() if k == "title"}, **attrs}
        elif child.type == "link_close" and hidden_links:
            child.hidden = hidden_links.pop()
        output.append(child)
    return output


def unique_anchor(base: str, anchors: set[str], counters: dict[str, int]) -> str:
    candidate = base
    while candidate in anchors:
        counters[base] = counters.get(base, 0) + 1
        candidate = f"{base}-{counters[base]}"
    anchors.add(candidate)
    return candidate


class RenderBudgetExceeded(Exception):
    pass


def spend(state: StateCore) -> None:
    if time.thread_time() > state.env.get("deadline", math.inf):
        raise RenderBudgetExceeded("cpu")


def limits_rule(state: StateCore) -> None:
    spend(state)
    headings = sum(token.type == "heading_open" for token in state.tokens)
    if headings > MARKDOWN_HEADING_LIMIT:
        raise RenderBudgetExceeded("headings")


def inline_rule(state: StateCore) -> None:
    for token in state.tokens:
        if token.type == "inline":
            spend(state)
            if token.children is None:
                token.children = []
            state.md.inline.parse(token.content, state.md, state.env, token.children)


def site_rule(state: StateCore) -> None:
    spend(state)
    context: MarkdownContext = state.env["context"]
    anchors: set[str] = set()
    counters: dict[str, int] = {}
    for index, token in enumerate(state.tokens):
        if token.type == "heading_open":
            inline = state.tokens[index + 1]
            slug = unique_anchor(slugify(inline_text(inline)), anchors, counters)
            token.attrSet("id", ANCHOR_PREFIX + slug)
            level = min(int(token.tag[1]) + HEADING_SHIFT, 6)
            token.tag = f"h{level}"
            state.tokens[index + 2].tag = f"h{level}"
        elif token.type in ("th_open", "td_open"):
            alignment = ALIGNMENTS.get(str(token.attrs.pop("style", "")).replace(" ", ""))
            if alignment:
                token.attrSet("class", f"align-{alignment}")
    spend(state)
    for token in state.tokens:
        if token.type == "inline" and token.children:
            token.children = rewrite_inline(token.children, context, anchors)


def table_open(*_: Any) -> str:
    return '<div class="md-table" tabindex="0" role="region" aria-label="Table">\n<table>\n'


def table_close(*_: Any) -> str:
    return "</table>\n</div>\n"


def focusable_code(renderer: RendererHTML, rule: str) -> Callable[..., str]:
    original = getattr(renderer, rule)

    def wrapped(tokens: Any, idx: int, options: Any, env: Any) -> str:
        return original(tokens, idx, options, env).replace("<pre>", '<pre tabindex="0">', 1)

    return wrapped


def render_env(context: MarkdownContext) -> dict[str, Any]:
    return {"context": context, "deadline": time.thread_time() + MARKDOWN_CPU_BUDGET}


def build_markdown() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})
    md.enable(["table", "strikethrough"])
    md.validateLink = safe_link
    md.core.ruler.after("block", "alexandria_limits", limits_rule)
    md.core.ruler.at("inline", inline_rule)
    md.core.ruler.push("alexandria_site", site_rule)
    md.renderer.rules["table_open"] = table_open
    md.renderer.rules["table_close"] = table_close
    for rule in ("fence", "code_block"):
        md.renderer.rules[rule] = focusable_code(md.renderer, rule)
    md.render(WARMUP_DOCUMENT, render_env(MarkdownContext("", "", frozenset())))
    return md


MARKDOWN = build_markdown()
MARKDOWN_CACHE: OrderedDict[str, Markup] = OrderedDict()
MARKDOWN_PENDING: dict[str, asyncio.Task[Markup]] = {}
MARKDOWN_POOL = ThreadPoolExecutor(max_workers=MARKDOWN_WORKERS, thread_name_prefix="markdown")


class ShortTimeslices:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.saved = sys.getswitchinterval()

    def __enter__(self) -> None:
        with self.lock:
            if self.active == 0:
                self.saved = sys.getswitchinterval()
                sys.setswitchinterval(MARKDOWN_TIMESLICE)
            self.active += 1

    def __exit__(self, *_: object) -> None:
        with self.lock:
            self.active -= 1
            if self.active == 0:
                sys.setswitchinterval(self.saved)


MARKDOWN_SLICES = ShortTimeslices()


def fingerprint(text: str, context: MarkdownContext) -> str:
    digest = hashlib.sha256()
    for part in (context.name, context.version, "\n".join(sorted(context.files)), text):
        digest.update(part.encode("utf-8", "surrogatepass"))
        digest.update(b"\x00")
    return digest.hexdigest()


def line_count(text: str) -> int:
    return text.count("\n") + text.count("\r") - text.count("\r\n") + 1


def plain_html(text: str) -> str:
    block = Token("code_block", "code", 0)
    block.content = reveal_bidi(text)
    code = MARKDOWN.renderer.render([block], MARKDOWN.options, {})
    return f'<p class="notice notice--info">{PLAIN_NOTICE}</p>\n{code}'


def format_markdown(text: str, context: MarkdownContext) -> str:
    if line_count(text) > MARKDOWN_LINE_LIMIT:
        raise RenderBudgetExceeded("lines")
    html = MARKDOWN.render(text, render_env(context))
    if len(html) > MARKDOWN_OUTPUT_LIMIT:
        raise RenderBudgetExceeded("output")
    return html


def render_html(text: str, context: MarkdownContext) -> Markup:
    try:
        html = format_markdown(text, context)
    except RenderBudgetExceeded as exc:
        logger.info(
            f"Markdown of {context.name} {context.version} shown as plain text",
            extra={
                "extra_data": {
                    "name": context.name,
                    "version": context.version,
                    "reason": str(exc),
                }
            },
        )
        html = plain_html(text)
    return Markup(html)


def retrieve(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
        task.exception()


async def render_and_store(key: str, text: str, context: MarkdownContext) -> Markup:
    task = asyncio.current_task()
    try:
        with MARKDOWN_SLICES:
            html = await asyncio.get_running_loop().run_in_executor(
                MARKDOWN_POOL, render_html, text, context
            )
        MARKDOWN_CACHE[key] = html
        while len(MARKDOWN_CACHE) > MARKDOWN_CACHE_SIZE:
            MARKDOWN_CACHE.popitem(last=False)
        return html
    finally:
        if MARKDOWN_PENDING.get(key) is task:
            del MARKDOWN_PENDING[key]


async def render_markdown(text: str, context: MarkdownContext) -> Markup:
    key = fingerprint(text, context)
    cached = MARKDOWN_CACHE.get(key)
    if cached is not None:
        MARKDOWN_CACHE.move_to_end(key)
        return cached
    loop = asyncio.get_running_loop()
    pending = MARKDOWN_PENDING.get(key)
    if pending is None or pending.get_loop() is not loop:
        pending = loop.create_task(render_and_store(key, text, context))
        pending.add_done_callback(retrieve)
        MARKDOWN_PENDING[key] = pending
    return await asyncio.shield(pending)


def metadata_items(metadata: dict[str, Any]) -> list[tuple[str, str]]:
    items = []
    for key in sorted(metadata)[:MAX_METADATA_ITEMS]:
        value = metadata[key]
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        if len(text) > MAX_METADATA_LENGTH:
            text = text[:MAX_METADATA_LENGTH] + "..."
        items.append((key, text))
    return items


def alx_block(*commands: str, private: bool = False) -> str:
    lines = [f"export ALX_URL={shlex.quote(settings.server.public_url)}"]
    if private:
        lines.append("export ALX_TOKEN=alx_your_token")
    return "\n".join([*lines, *commands])


def alx_reference(name: str, version: str | None = None) -> str:
    return shlex.quote(name if version is None else f"{name}@{version}")


def use_snippets(name: str, version: str, sha256: str) -> list[dict[str, str]]:
    public_url = settings.server.public_url
    archive = f"{name}-{version}.zip"
    url = f"{public_url}{download_path(name, version)}"
    arguments = json.dumps({"name": name, "version": version}, ensure_ascii=False)
    return [
        {
            "id": "use-mcp",
            "label": "MCP tool call",
            "text": f"get_entrypoint {arguments}",
        },
        {
            "id": "use-curl",
            "label": "curl",
            "text": (
                f"curl -fL -o {shlex.quote(archive)} {shlex.quote(url)}\n"
                f"echo {shlex.quote(f'{sha256}  {archive}')} | sha256sum -c -"
            ),
        },
        {
            "id": "use-alx",
            "label": "alx",
            "text": alx_block(f"alx pull {alx_reference(name, version)}"),
        },
    ]


def connect_snippets(example: str, private: bool) -> dict[str, str]:
    base = settings.server.public_url
    header = ' -H "Authorization: Bearer $ALX_TOKEN"' if private else ""
    call = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "search_skills", "arguments": {"query": "pdf", "limit": 5}},
        }
    )
    return {
        "url": f"{base}/mcp",
        "add": f"claude mcp add --transport http alexandria {base}/mcp",
        "call": (
            f"curl -s {base}/mcp{header} \\\n"
            "  -H 'Content-Type: application/json' \\\n"
            "  -H 'Accept: application/json, text/event-stream' \\\n"
            f"  -d {shlex.quote(call)}"
        ),
        "search": f'curl -s "{base}/api/v1/skills?q=pdf&kind=skill"{header}',
        "entrypoint": (
            f"curl -s {base}/api/v1/skills/{quote(example, safe='')}/versions/latest/entrypoint"
            f"{header}"
        ),
        "download": f"curl -fLO {base}{download_path(example, 'latest')}{header}",
        "alx": alx_block(
            "alx search pdf --kind skill", f"alx pull {alx_reference(example)}", private=private
        ),
    }


@route("/")
async def landing(request: Request, db: DbSession) -> Response:
    skills = SkillService(db)
    counts: dict[str, int] | None = None
    recent = []
    try:
        counts = {kind: (await skills.search(None, kind=kind, limit=1)).total for kind in KINDS}
        recent = (await skills.search(None, sort="updated", limit=RECENT_ENTRIES)).items
    except UnauthorizedError:
        counts = None
    finally:
        await release_connection(db)
    example = recent[0].name if recent else PLACEHOLDER_NAME
    return render(
        request,
        "index.html",
        {
            "counts": counts,
            "total": sum(counts.values()) if counts else 0,
            "recent": recent,
            "connect": connect_snippets(example, counts is None),
            "private": counts is None,
            "canonical": f"{settings.server.public_url}/",
        },
    )


@route("/registry")
@route("/registry/")
async def registry(
    request: Request,
    db: DbSession,
    q: str | None = None,
    kind: str | None = None,
    category: str | None = None,
    sort: str | None = None,
    tag: Annotated[list[str] | None, Query(max_length=MAX_TAG_FILTERS)] = None,
    page: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 1,
) -> Response:
    filters = Filters.parse(q, kind, category, sort, tag, page)
    skills = SkillService(db)
    try:
        result = await skills.search(
            None,
            q=filters.q,
            kind=filters.kind,
            tags=filters.tags,
            category=filters.category,
            sort=filters.sort,
            limit=PAGE_SIZE,
            offset=(filters.page - 1) * PAGE_SIZE,
        )
        tags = await skills.tags(None, kind=filters.kind, limit=TAG_FACETS)
        categories = await skills.categories(None, kind=filters.kind, limit=CATEGORY_FACETS)
    finally:
        await release_connection(db)
    sorts = ["name", "updated", "created"]
    if filters.q:
        sorts.insert(0, "relevance")
    return render(
        request,
        "registry.html",
        {
            "nav": "registry",
            "filters": filters,
            "result": result,
            "chips": filters.chips(),
            "kinds": [{"name": name, "url": filters.url(kind=name, page=1)} for name in KINDS],
            "all_url": filters.url(kind="", page=1),
            "tag_facets": tag_facets(tags, filters),
            "category_facets": category_facets(categories, filters),
            "pager": pagination(filters, result.total),
            "sorts": sorts,
            "default_sort": "relevance" if filters.q else "name",
            "canonical": f"{settings.server.public_url}/registry",
        },
    )


@route("/registry/skills/{ref}")
async def skill_detail(
    request: Request,
    ref: str,
    db: DbSession,
    store: Store,
    version: str | None = None,
) -> Response:
    try:
        skill = await SkillService(db).get(None, ref)
        info = await VersionService(db).get(None, skill.name, version or "latest")
        packages = PackageService(db, store)
        entry = await packages.entrypoint(None, skill.name, info.version)
        listing = await packages.manifest(None, skill.name, info.version)
    finally:
        await release_connection(db)
    source, clipped = clip_markdown(strip_frontmatter(entry.content or ""))
    context = MarkdownContext(skill.name, info.version, frozenset(f.path for f in listing.files))
    body = await render_markdown(source, context)
    shown = limit_files(listing.files, None)
    return render(
        request,
        "skill_detail.html",
        {
            "nav": "registry",
            "skill": skill,
            "info": info,
            "entry": entry,
            "body": body,
            "clipped": clipped,
            "tree_nodes": build_tree(shown, None, entry.path),
            "tree_shown": len(shown),
            "file_count": len(listing.files),
            "metadata": metadata_items(info.metadata),
            "snippets": use_snippets(skill.name, info.version, info.sha256),
            "outdated": skill.latest_version is not None and info.version != skill.latest_version,
            "canonical": f"{settings.server.public_url}{skill_url(skill.name)}",
        },
    )


@route("/registry/skills/{ref}/versions/{version}/files/{path:path}")
async def file_view(
    request: Request,
    ref: str,
    version: str,
    path: str,
    db: DbSession,
    store: Store,
) -> Response:
    packages = PackageService(db, store)
    try:
        listing = await packages.manifest(None, ref, version)
        content = await packages.read_file(None, listing.name, listing.version, path)
    finally:
        await release_connection(db)
    shown = limit_files(listing.files, content.path)
    return render(
        request,
        "file.html",
        {
            "nav": "registry",
            "name": listing.name,
            "version": listing.version,
            "file": content,
            "text": reveal_bidi(content.content) if content.content is not None else None,
            "tree_nodes": build_tree(shown, content.path, ""),
            "tree_shown": len(shown),
            "file_count": len(listing.files),
            "robots": "noindex",
        },
    )


@route("/registry/{rest:path}")
async def unknown(request: Request, rest: str) -> Response:
    raise NotFoundError("There is no such page in the registry")
