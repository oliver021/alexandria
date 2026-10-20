from __future__ import annotations

import base64
import functools
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field

from app.config.settings import settings
from app.logging import get_logger
from app.mcp import context
from app.models.schemas import (
    DownloadInfo,
    EntrypointContent,
    FileContent,
    FileListing,
    KindName,
    SkillDetail,
    TaxonomyItem,
    UploadResult,
    VersionSummary,
)
from app.services import access
from app.services.catalog import (
    LATEST,
    MAX_FILTER_LENGTH,
    MAX_OFFSET,
    MAX_QUERY_LENGTH,
    MAX_TAXONOMY_SIZE,
)
from app.services.errors import DomainError, PayloadTooLargeError, StorageError, ValidationError
from app.services.package_service import (
    PackageService,
    encoded_package_limit,
    limits_from_settings,
)
from app.services.skill_service import SkillPage, SkillService
from app.services.version_service import VersionService

logger = get_logger("app.mcp.tools")

MAX_SEARCH_LIMIT = 50
DEFAULT_SEARCH_LIMIT = 10
DEFAULT_TAXONOMY_LIMIT = 50
NO_TAGS: tuple[str, ...] = ()
STORAGE_FAILURE = "STORAGE_ERROR: internal storage error"
INTERNAL_FAILURE = "INTERNAL_ERROR: internal error"
OK_OUTCOME = "ok"
INTERNAL_OUTCOME = "INTERNAL_ERROR"
INVALID_BASE64_MESSAGE = (
    "content_base64 must be valid base64 (standard alphabet, a single line, no whitespace)"
)

ToolFunction = Callable[..., Awaitable[Any]]
ToolOutput = BaseModel | list[TaxonomyItem]


def paragraphs(*parts: str) -> str:
    return "\n\n".join(parts)


KINDS_TEXT = (
    "There are two kinds of entry. A 'skill' (kind 'skill') is procedural: its entry point "
    "SKILL.md tells you how to do something. A 'grounding' (kind 'grounding') is referential: "
    "its entry point INDEX.md maps the documents, tables or data you can consult."
)
LATEST_TEXT = (
    "`version` defaults to 'latest': the highest published version, where stable releases win "
    "over prereleases and draft or deprecated versions are never 'latest'. Pass an exact "
    "version such as '1.2.0' to pin one; a deprecated version can still be read that way."
)
LATEST_SHORT_TEXT = (
    "`version` is an exact version or 'latest' (the default), the highest published version "
    "as explained in get_entrypoint."
)
NEXT_FILES_TEXT = (
    "The entry point usually points to other files of the package: call list_files to see all "
    "of them and read_file to open one."
)

SEARCH_SKILLS_DESCRIPTION = paragraphs(
    "Find entries in the Alexandria registry. Use it first, whenever you need a capability, "
    "a procedure or a reference and you do not know its exact name.",
    "Every space-separated word of `query` must match somewhere in the name, display name, "
    "description, tags or entry-point text (case-insensitive) and results are ranked best "
    "match first; an empty query lists every visible entry sorted by name. Narrow it with "
    "`kind`, `tags` (all of them must be present) and `category` (list_tags and "
    "list_categories show the values in use), and page through it with `limit` (1 to 50, "
    "default 10) and `offset`.",
    "Returns {items, total, limit, offset}. Each item summarizes an entry (name, display_name, "
    "description, kind, category, tags, status, latest_version, owner_id, created_at, "
    "updated_at) but not its content; `total` counts every match, not only this page.",
    KINDS_TEXT,
    "Next step: get_entrypoint with the name of an item to read its SKILL.md or INDEX.md, or "
    "get_skill to see all its versions.",
)
GET_SKILL_DESCRIPTION = paragraphs(
    "Get one entry by name (or UUID): description, kind, category, tags, status, "
    "latest_version and every version you may see, highest first, each with its status "
    "(published, deprecated, or draft for the owner), sha256, size, file count and dates.",
    "Use it after search_skills to compare versions, to check whether a version is deprecated "
    "or to pick an exact version to pin. It does not return file contents: use "
    "get_entrypoint for that. Draft versions and hidden entries are only visible to their "
    "owner and to admins; for anyone else they answer NOT_FOUND.",
)
GET_ENTRYPOINT_DESCRIPTION = paragraphs(
    "Read the entry point of a package: SKILL.md for a skill (the instructions to follow) or "
    "INDEX.md for a grounding (the map of the reference material). It is the usual second "
    "step after search_skills.",
    "Returns the resolved name, version and kind plus the file: path, size, sha256, "
    "media_type, text, content (Markdown with its YAML frontmatter), truncated and encoding. "
    "If the file is longer than the server's inline limit, content is cut at a character "
    "boundary and truncated is true.",
    LATEST_TEXT,
    NEXT_FILES_TEXT,
)
LIST_FILES_DESCRIPTION = paragraphs(
    "List every file of a package version (its manifest): path, size, sha256, media_type and "
    "a `text` flag that tells whether read_file can return the content inline. Returns "
    "{name, version, files} with the resolved version and the files sorted by path.",
    "Use it after get_entrypoint to discover the scripts, templates, tables, specifications "
    "or data bundled with the package.",
    LATEST_SHORT_TEXT,
)
READ_FILE_DESCRIPTION = paragraphs(
    "Read one file of a package version by its path. Text files come back inline in "
    "`content`; a text file larger than the server's inline limit is cut at a character "
    "boundary and `truncated` is true. Binary files (`text` is false, for example SQLite "
    "databases or images) return only their metadata with `content` null: get the whole "
    "package with get_download_info instead.",
    "`path` must be one of the paths returned by list_files: relative to the package root, "
    "with forward slashes. Absolute paths, '..' segments and backslashes fail with "
    "'VALIDATION_ERROR: ... [INVALID_PATH]'; a path that is not in the package fails with "
    "NOT_FOUND.",
    "Returns {path, size, sha256, media_type, text, content, truncated, encoding}.",
    LATEST_SHORT_TEXT,
)
GET_DOWNLOAD_INFO_DESCRIPTION = paragraphs(
    "Get what you need to download a complete package: the absolute URL of its zip, its "
    "sha256, its size in bytes and `requires_auth`, which tells whether the download needs "
    "the same 'Authorization: Bearer' credential you are using here. Binary content never "
    "travels through MCP: fetch the URL with an HTTP GET and check the sha256.",
    "Use it when you need the whole package, for example to run bundled scripts or to open "
    "binary files. Returns {name, version, sha256, size_bytes, url, requires_auth}.",
    LATEST_SHORT_TEXT,
)
LIST_TAGS_DESCRIPTION = paragraphs(
    "List the tags used by the entries you can see, with the number of entries that carry "
    "each one, most used first. Use it to discover valid values for the `tags` filter of "
    "search_skills.",
    "`kind` restricts the count to skills or groundings and `limit` caps the number of tags "
    "(default 50). Returns a JSON array of {name, count}.",
)
LIST_CATEGORIES_DESCRIPTION = paragraphs(
    "List the categories used by the entries you can see, with the number of entries in each "
    "one, most used first. Use it to discover valid values for the `category` filter of "
    "search_skills.",
    "`kind` restricts the count to skills or groundings and `limit` caps the number of "
    "categories (default 50). Returns a JSON array of {name, count}.",
)
UPLOAD_PACKAGE_DESCRIPTION = paragraphs(
    "Upload a package to the registry. Requires a credential with write scope.",
    "`content_base64` is a .zip file encoded as base64 (standard alphabet, one line, no "
    "whitespace). The zip needs exactly one entry point at its root: SKILL.md for a skill or "
    "INDEX.md for a grounding (a single wrapper directory is tolerated). The entry point "
    "starts with YAML frontmatter between '---' lines holding at least `name` (lowercase "
    "letters, digits and hyphens, up to 64 characters), `description` and `version` (a "
    "semantic version; you can pass it in `version` instead); `display_name`, `tags` and "
    "`category` are optional.",
    "The first upload of a name creates the entry and makes you its owner. Later uploads of "
    "that name, by the owner or an admin, add a new immutable version; a version can never "
    "be replaced, so upload a new version number to change anything, and an entry never "
    "changes kind. The version is stored as a draft, visible only to the owner and to "
    "admins, unless `publish` is true or the registry publishes automatically; publish a "
    "draft later with publish_version.",
    "Returns {skill, version}. Invalid packages fail with 'INVALID_PACKAGE: ... [REASON]' "
    "(for example UNSAFE_PATH, MISSING_ENTRYPOINT, BAD_FRONTMATTER or TOO_LARGE), a version "
    "that already exists fails with CONFLICT and an upload over the size limit fails with "
    "PAYLOAD_TOO_LARGE.",
)
PUBLISH_VERSION_DESCRIPTION = paragraphs(
    "Publish a draft version, or publish a deprecated one again, so that everybody can read "
    "it and it can become 'latest'. The entry becomes visible to everybody when its first "
    "version is published. Requires write scope and being the owner of the entry or an "
    "admin. Publishing a version that is already published changes nothing.",
    "`version` must be exact (the literal 'latest' is not accepted). Returns the version summary.",
)
DEPRECATE_VERSION_DESCRIPTION = paragraphs(
    "Mark a published version as deprecated: it stays readable and downloadable by exact "
    "version but it is never 'latest' again, so agents that resolve 'latest' move on to the "
    "next published version. Requires write scope and being the owner of the entry or an "
    "admin.",
    "The only published version of an entry cannot be deprecated (CONFLICT): publish another "
    "version first. Deprecating a version that is already deprecated changes nothing, and "
    "publish_version undoes a deprecation. `version` must be exact. Returns the version "
    "summary.",
)

NameParam = Annotated[
    str,
    Field(
        description=(
            "Name of the entry: its unique lowercase slug, for example 'pdf-forms', as "
            "returned by search_skills. The entry's UUID is accepted too."
        )
    ),
]
VersionParam = Annotated[
    str,
    Field(
        description=(
            "Exact semantic version such as '1.2.0', or 'latest' (the default) for the "
            "highest published version."
        )
    ),
]
ExactVersionParam = Annotated[
    str,
    Field(description="Exact semantic version such as '1.2.0'. The literal 'latest' is not valid."),
]
PathParam = Annotated[
    str,
    Field(
        description=(
            "Path of a file inside the package exactly as listed by list_files: relative to "
            "the package root, with forward slashes, for example 'scripts/extract.py'."
        )
    ),
]
QueryParam = Annotated[
    str,
    Field(
        max_length=MAX_QUERY_LENGTH,
        description=(
            "Free-text search. Words are separated by spaces and every word must match the "
            "name, display name, description, tags or entry-point text, ignoring case. Empty "
            "lists every visible entry."
        ),
    ),
]
KindParam = Annotated[
    KindName | None,
    Field(
        description=(
            "Only entries of this kind: 'skill' (procedural, SKILL.md) or 'grounding' "
            "(referential, INDEX.md). Omit for both."
        )
    ),
]
TagsParam = Annotated[
    list[Annotated[str, Field(max_length=MAX_FILTER_LENGTH)]],
    Field(
        description=(
            "Only entries that carry all of these tags (lowercase, as returned by list_tags). "
            "Empty means no tag filter."
        )
    ),
]
CategoryParam = Annotated[
    str | None,
    Field(
        max_length=MAX_FILTER_LENGTH,
        description=(
            "Only entries in this exact category (a lowercase slug, as returned by "
            "list_categories). Omit for any category."
        ),
    ),
]
SearchLimitParam = Annotated[
    int,
    Field(
        strict=True,
        ge=1,
        le=MAX_SEARCH_LIMIT,
        description=f"Maximum number of entries to return, from 1 to {MAX_SEARCH_LIMIT}.",
    ),
]
OffsetParam = Annotated[
    int,
    Field(
        strict=True,
        ge=0,
        le=MAX_OFFSET,
        description="Number of matching entries to skip, to read the next page.",
    ),
]
TaxonomyLimitParam = Annotated[
    int,
    Field(
        strict=True,
        ge=1,
        le=MAX_TAXONOMY_SIZE,
        description=f"Maximum number of items to return, from 1 to {MAX_TAXONOMY_SIZE}.",
    ),
]
ContentParam = Annotated[
    str,
    Field(
        description=(
            "The package: a .zip file encoded as base64 (standard alphabet, one line, no "
            "whitespace)."
        )
    ),
]
UploadVersionParam = Annotated[
    str | None,
    Field(
        description=(
            "Semantic version of the package. Required unless the entry point declares "
            "`version`; when both are given they must be equal."
        )
    ),
]
PublishParam = Annotated[
    bool,
    Field(
        strict=True,
        description=(
            "true publishes the version right away. false (the default) leaves it as a draft, "
            "unless the registry is configured to publish uploads automatically."
        ),
    ),
]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    title: str
    description: str
    annotations: ToolAnnotations
    function: ToolFunction


TOOLS: list[ToolSpec] = []


def describe(error: DomainError) -> str:
    text = f"{error.code}: {error.message}"
    if error.reason:
        text += f" [{error.reason}]"
    return text


def record_call(name: str, outcome: str, started: float) -> None:
    principal = context.current_principal()
    logger.info(
        f"MCP tool {name}: {outcome}",
        extra={
            "extra_data": {
                "tool": name,
                "outcome": outcome,
                "user_id": None if principal is None else principal.user_id,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            }
        },
    )


def failure(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=True)


def success(output: ToolOutput) -> CallToolResult:
    if isinstance(output, BaseModel):
        payload: Any = output.model_dump(mode="json")
        structured = payload
    else:
        payload = [item.model_dump(mode="json") for item in output]
        structured = {"result": payload}
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return CallToolResult(
        content=[TextContent(type="text", text=text)], structured_content=structured
    )


def tool(
    *, title: str, description: str, read_only: bool, idempotent: bool = True
) -> Callable[[ToolFunction], ToolFunction]:
    if read_only:
        annotations = ToolAnnotations(read_only_hint=True, open_world_hint=False)
    else:
        annotations = ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=idempotent,
            open_world_hint=False,
        )

    def register(function: ToolFunction) -> ToolFunction:
        @functools.wraps(function)
        async def run(*args: Any, **kwargs: Any) -> CallToolResult:
            started = time.perf_counter()
            try:
                result = success(await function(*args, **kwargs))
                outcome = OK_OUTCOME
            except StorageError as error:
                result, outcome = failure(STORAGE_FAILURE), error.code
            except DomainError as error:
                result, outcome = failure(describe(error)), error.code
            except Exception:
                logger.exception(f"MCP tool {function.__name__} failed unexpectedly")
                result, outcome = failure(INTERNAL_FAILURE), INTERNAL_OUTCOME
            record_call(function.__name__, outcome, started)
            return result

        TOOLS.append(
            ToolSpec(
                name=function.__name__,
                title=title,
                description=description,
                annotations=annotations,
                function=run,
            )
        )
        return run

    return register


@asynccontextmanager
async def skills() -> AsyncIterator[SkillService]:
    async with context.session_scope() as db:
        yield SkillService(db)


@asynccontextmanager
async def versions() -> AsyncIterator[VersionService]:
    async with context.session_scope() as db:
        yield VersionService(db)


@asynccontextmanager
async def packages() -> AsyncIterator[PackageService]:
    async with context.session_scope() as db:
        yield PackageService(db, context.get_blob_store())


def decode_package(content: str) -> bytes:
    limits = limits_from_settings(settings.registry.limits)
    if len(content) > encoded_package_limit(limits):
        logger.warning(
            "MCP upload rejected before decoding: payload too large",
            extra={
                "extra_data": {
                    "encoded_length": len(content),
                    "limit": limits.max_package_bytes,
                }
            },
        )
        raise PayloadTooLargeError(
            f"The package exceeds the limit of {limits.max_package_bytes} bytes"
        )
    try:
        return base64.b64decode(content, validate=True)
    except ValueError as error:
        raise ValidationError(INVALID_BASE64_MESSAGE) from error


@tool(title="Search the registry", description=SEARCH_SKILLS_DESCRIPTION, read_only=True)
async def search_skills(
    query: QueryParam = "",
    kind: KindParam = None,
    tags: TagsParam = NO_TAGS,
    category: CategoryParam = None,
    limit: SearchLimitParam = DEFAULT_SEARCH_LIMIT,
    offset: OffsetParam = 0,
) -> SkillPage:
    async with skills() as service:
        return await service.search(
            context.current_principal(),
            q=query,
            kind=kind,
            tags=tags,
            category=category,
            limit=limit,
            offset=offset,
        )


@tool(title="Get an entry", description=GET_SKILL_DESCRIPTION, read_only=True)
async def get_skill(name: NameParam) -> SkillDetail:
    async with skills() as service:
        return await service.get(context.current_principal(), name)


@tool(title="Read the entry point", description=GET_ENTRYPOINT_DESCRIPTION, read_only=True)
async def get_entrypoint(name: NameParam, version: VersionParam = LATEST) -> EntrypointContent:
    async with packages() as service:
        return await service.entrypoint(context.current_principal(), name, version)


@tool(title="List the files of a package", description=LIST_FILES_DESCRIPTION, read_only=True)
async def list_files(name: NameParam, version: VersionParam = LATEST) -> FileListing:
    async with packages() as service:
        return await service.manifest(context.current_principal(), name, version)


@tool(title="Read a file of a package", description=READ_FILE_DESCRIPTION, read_only=True)
async def read_file(
    name: NameParam, path: PathParam, version: VersionParam = LATEST
) -> FileContent:
    async with packages() as service:
        return await service.read_file(context.current_principal(), name, version, path)


@tool(
    title="Get download information",
    description=GET_DOWNLOAD_INFO_DESCRIPTION,
    read_only=True,
)
async def get_download_info(name: NameParam, version: VersionParam = LATEST) -> DownloadInfo:
    async with packages() as service:
        return await service.download_info(context.current_principal(), name, version)


@tool(title="List tags", description=LIST_TAGS_DESCRIPTION, read_only=True)
async def list_tags(
    kind: KindParam = None, limit: TaxonomyLimitParam = DEFAULT_TAXONOMY_LIMIT
) -> list[TaxonomyItem]:
    async with skills() as service:
        return await service.tags(context.current_principal(), kind=kind, limit=limit)


@tool(title="List categories", description=LIST_CATEGORIES_DESCRIPTION, read_only=True)
async def list_categories(
    kind: KindParam = None, limit: TaxonomyLimitParam = DEFAULT_TAXONOMY_LIMIT
) -> list[TaxonomyItem]:
    async with skills() as service:
        return await service.categories(context.current_principal(), kind=kind, limit=limit)


@tool(
    title="Upload a package",
    description=UPLOAD_PACKAGE_DESCRIPTION,
    read_only=False,
    idempotent=False,
)
async def upload_package(
    content_base64: ContentParam,
    version: UploadVersionParam = None,
    publish: PublishParam = False,
) -> UploadResult:
    principal = access.require_write(context.current_principal())
    data = decode_package(content_base64)
    async with packages() as service:
        return await service.upload(
            principal, data, version=version, publish=True if publish else None
        )


@tool(title="Publish a version", description=PUBLISH_VERSION_DESCRIPTION, read_only=False)
async def publish_version(name: NameParam, version: ExactVersionParam) -> VersionSummary:
    async with versions() as service:
        return await service.publish(context.current_principal(), name, version)


@tool(title="Deprecate a version", description=DEPRECATE_VERSION_DESCRIPTION, read_only=False)
async def deprecate_version(name: NameParam, version: ExactVersionParam) -> VersionSummary:
    async with versions() as service:
        return await service.deprecate(context.current_principal(), name, version)


def register_tools(server: MCPServer) -> None:
    for spec in TOOLS:
        server.add_tool(
            spec.function,
            name=spec.name,
            title=spec.title,
            description=spec.description,
            annotations=spec.annotations,
        )
