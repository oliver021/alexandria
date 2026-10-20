from __future__ import annotations

import importlib.metadata
from functools import lru_cache

from mcp.server.mcpserver import MCPServer

from app.mcp.tools import paragraphs, register_tools

SERVER_NAME = "alexandria"
SERVER_TITLE = "Alexandria"
SERVER_DESCRIPTION = "Registry of skills and grounding packages for AI agents."
DISTRIBUTION_NAME = "alexandria"
UNKNOWN_VERSION = "0.0.0"

SERVER_INSTRUCTIONS = paragraphs(
    "Alexandria is a registry of versioned packages (zip files) for AI agents. A 'skill' is "
    "procedural: its entry point SKILL.md tells you how to do something. A 'grounding' is "
    "referential: its entry point INDEX.md maps reference material (documents, tables, "
    "schemas, data). Both can bundle extra files such as scripts, templates or data.",
    "Typical flow: search_skills to find an entry, get_entrypoint to read its SKILL.md or "
    "INDEX.md, then list_files and read_file for the files it points to. Use "
    "get_download_info when you need the whole package, because binaries never travel "
    "through MCP.",
    "Versions are immutable semantic versions and 'latest' means the highest published one "
    "(stable releases win over prereleases; drafts and deprecated versions are never "
    "'latest'). Pin an exact version when you need reproducible behaviour.",
    "Reading is public unless the operator requires a credential. upload_package, "
    "publish_version and deprecate_version need an 'Authorization: Bearer' API token or JWT "
    "with write scope, and only the owner of an entry or an admin can change it.",
    "Failures come back as tool errors whose text is 'CODE: message', optionally followed by "
    "' [REASON]'. Codes: NOT_FOUND, VALIDATION_ERROR, INVALID_PACKAGE, CONFLICT, FORBIDDEN, "
    "UNAUTHORIZED, PAYLOAD_TOO_LARGE, STORAGE_ERROR, INTERNAL_ERROR.",
)


@lru_cache(maxsize=1)
def server_version() -> str:
    try:
        return importlib.metadata.version(DISTRIBUTION_NAME)
    except importlib.metadata.PackageNotFoundError:
        return UNKNOWN_VERSION


def build_server() -> MCPServer:
    server = MCPServer(
        SERVER_NAME,
        title=SERVER_TITLE,
        description=SERVER_DESCRIPTION,
        instructions=SERVER_INSTRUCTIONS,
        version=server_version(),
        subscriptions=False,
    )
    register_tools(server)
    return server
