# Alexandria — Architecture & Implementation Plan

> A lightweight skill/capability registry built around MCP, providing a common discovery and registration layer for AI agents and applications.

---

## Table of Contents

1. [Executive Architecture Overview](#1-executive-architecture-overview)
2. [Core Domain Model](#2-core-domain-model)
3. [Application/Service Layer](#3-applicationservice-layer)
4. [REST Interface](#4-rest-interface)
5. [MCP Interface](#5-mcp-interface)
6. [Database Architecture](#6-database-architecture)
7. [Authentication Architecture](#7-authentication-architecture)
8. [Configuration Model](#8-configuration-model)
9. [Admin Frontend](#9-admin-frontend)
10. [Public Landing Page](#10-public-landing-page)
11. [Public Registry](#11-public-registry)
12. [Repository/Project Structure](#12-repositoryproject-structure)
13. [Security Model](#13-security-model)
14. [Testing Strategy](#14-testing-strategy)
15. [Deployment Model](#15-deployment-model)
16. [Phased Implementation Roadmap](#16-phased-implementation-roadmap)
17. [MVP vs Later-Feature Matrix](#17-mvp-vs-later-feature-matrix)
18. [Key Architectural Decisions and Trade-offs](#18-key-architectural-decisions-and-trade-offs)
19. [Risks and Failure Modes](#19-risks-and-failure-modes)
20. [Recommended First Milestone](#20-recommended-first-milestone)

---

## 1. Executive Architecture Overview

### What Alexandria Is

Alexandria is a **registry** — a central catalog where skills/capabilities offered by AI agents and applications are described, versioned, and discovered. It is not an orchestration platform, a workflow engine, or a plugin marketplace. It answers one question: *"What capabilities exist, and how do I use them?"*

### Core Principle

> **One domain/application service layer, multiple interfaces.**

All business logic lives in a single application service layer. Both the REST API and MCP interface are thin adapters that call into this shared layer. This is the most important architectural constraint in the system.

### High-Level Architecture

```
                    ┌──────────────────────────────┐
                    │       Public Browser          │
                    │     (HTML registry/landing)   │
                    └──────────────┬───────────────┘
                                   │ HTTP
┌──────────────┐        ┌─────────┴─────────────┐        ┌──────────────┐
│  MCP Client  │───────▸│  Alexandria FastAPI    │◂───────│  MCP Client  │
│  (agent/app) │  MCP   │  Application Service   │  MCP   │  (agent/app) │
└──────────────┘        │  Layer                 │        └──────────────┘
                        │                        │
                        │  ┌──────────────────┐  │
                        │  │   REST Router     │  │
                        │  │   MCP Server      │  │
                        │  │   Auth Provider   │  │
                        │  └────────┬─────────┘  │
                        └───────────┼────────────┘
                                    │
                         ┌──────────┴──────────┐
                         │  SQLAlchemy (async)  │
                         │  aiosqlite / asyncpg  │
                         └──────────┬──────────┘
                                    │
                              ┌─────┴─────┐
                              │  SQLite    │ ← MVP
                              │  PostgreSQL│ ← Later
                              └───────────┘
```

A single FastAPI process serves:
- **REST API** (JSON over HTTP)
- **MCP server** (via SSE transport over HTTP)
- **Admin frontend** (Svelte SPA, served as static files)
- **Public site** (server-rendered or static HTML)

One process, one database connection pool, one service layer.

### What Makes This Different From "Just a REST API"

The MCP interface is not a second API with its own logic. It is a protocol adapter over the same use cases. When a change is made to the service layer, both REST and MCP get the same behavior for free. This is validated by shared test suites.

---

## 2. Core Domain Model

### Design Philosophy

Model only what is needed to answer the registry's core questions:

1. **What skills/capabilities exist?**
2. **What does each skill do?**
3. **What version is available?**
4. **How do I invoke/connect to it?**
5. **Who published it?**

### Entities

#### `Skill`

The central entity. A skill is a named, versioned bundle of capabilities offered by a publisher.

| Field | Type | Notes |
|---|---|---|
| `id` | UUID | Primary key, immutable |
| `name` | text | Unique, machine-friendly identifier (e.g. `web-search`, `code-review`) |
| `display_name` | text | Human-readable name |
| `description` | text | What this skill does |
| `owner_id` | FK → User | Publisher/owner |
| `status` | enum | `draft`, `published`, `unpublished` |
| `created_at` | timestamp | |
| `updated_at` | timestamp | |

#### `SkillVersion`

A versioned snapshot of a skill's definition. A skill always has at least one version.

| Field | Type | Notes |
|---|---|---|
| `id` | UUID | Primary key |
| `skill_id` | FK → Skill | |
| `version` | text | Semantic version string (e.g. `1.0.0`) |
| `capabilities` | JSON | Array of capability definitions |
| `endpoints` | JSON | How to invoke this skill (MCP server URL, transport type, etc.) |
| `metadata` | JSON | Arbitrary key-value metadata |
| `tags` | text[] | Searchable tags |
| `status` | enum | `draft`, `published`, `deprecated` |
| `published_at` | timestamp | When this version was published |
| `created_at` | timestamp | |

The `capabilities` JSON array holds the actual capability definitions:

```json
[
  {
    "name": "web_search",
    "type": "tool",
    "description": "Search the web and return results",
    "parameters": {
      "query": { "type": "string", "description": "Search query" }
    }
  }
]
```

The `endpoints` JSON array describes connectivity:

```json
[
  {
    "type": "mcp",
    "transport": "sse",
    "url": "https://search-agent.example.com/mcp"
  }
]
```

#### `User`

| Field | Type | Notes |
|---|---|---|
| `id` | UUID | Primary key |
| `username` | text | Unique, used for login |
| `email` | text | Unique |
| `password_hash` | text | Bcrypt hash (local auth mode) |
| `role` | enum | `admin`, `publisher` |
| `is_active` | boolean | |
| `created_at` | timestamp | |

### What Is Explicitly NOT in the MVP

| Concept | Status |
|---|---|
| Separate `Capability` table | **Deferred.** Capabilities live as JSON within `SkillVersion`. A dedicated table is warranted only when capabilities need independent lifecycle, cross-skill queries, or fine-grained access control. |
| `Organization` / `Team` | **Deferred.** Ownership is per-user initially. Multi-org support is a later addition. |
| `Dependency` / `Compatibility` | **Later.** Skill-to-skill relationships. |
| `Rating` / `Review` | **Later.** Community feedback. |
| `Usage analytics` | **Later.** |

### Why JSON for Capabilities and Endpoints

In the MVP, capabilities and endpoints are **described data**, not independently queried entities. Storing them as JSON columns on `SkillVersion` avoids premature table proliferation while still allowing future extraction into dedicated tables. The registry queries by skill; it does not need to independently index every capability parameter.

---

## 3. Application/Service Layer

### Purpose

The service layer contains all use cases (business operations). It is the single source of truth for what the system can do. Both REST and MCP call into it.

### Use Cases

```
SkillService:
    create_skill(owner_id, data) → Skill
    update_skill(skill_id, data) → Skill
    list_skills(filters, pagination) → PaginatedResult[Skill]
    get_skill(skill_id) → Skill
    search_skills(query, filters) → PaginatedResult[Skill]

SkillVersionService:
    create_version(skill_id, data) → SkillVersion
    list_versions(skill_id) → list[SkillVersion]
    get_version(skill_id, version) → SkillVersion
    publish_version(skill_id, version) → SkillVersion
    unpublish_version(skill_id, version) → SkillVersion

AuthService:
    create_user(data) → User
    authenticate(username, password) → User | None
    get_user(user_id) → User
    list_users() → list[User]
    deactivate_user(user_id) → User
```

### Code Layout

```python
# app/services/skill_service.py

class SkillService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create_skill(self, owner_id: UUID, data: SkillCreate) -> Skill:
        # validation, repository call, event emission
        ...

    async def list_skills(
        self,
        status: SkillStatus | None = None,
        tag: str | None = None,
        owner_id: UUID | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> PaginatedResult[Skill]:
        ...
```

### Key Design Rules

1. **Services receive a database session, not a repository.** Repositories are thin data-access functions (not classes with complex state). This keeps the service layer testable by mocking the session.
2. **Services raise domain exceptions**, not HTTP exceptions. The REST router and MCP server translate exceptions to response codes / error formats.
3. **No framework dependency in services.** Services depend on SQLAlchemy async interfaces and Pydantic models, never on FastAPI request/response objects.
4. **Pagination is uniform.** Every list operation returns `PaginatedResult[T]` with `items`, `total`, `limit`, `offset`.

---

## 4. REST Interface

### Endpoint Matrix

| Use Case | Method | Path | Auth | Description |
|---|---|---|---|---|
| List skills | `GET` | `/api/v1/skills` | Public | Paginated list with filters |
| Get skill | `GET` | `/api/v1/skills/{id}` | Public | Full skill detail with latest version |
| Search skills | `GET` | `/api/v1/skills?q=...` | Public | Free-text search |
| Create skill | `POST` | `/api/v1/skills` | Publisher+ | Register new skill |
| Update skill | `PUT` | `/api/v1/skills/{id}` | Owner/Admin | Update skill metadata |
| Delete skill | `DELETE` | `/api/v1/skills/{id}` | Owner/Admin | Soft-delete (unpublish) |
| List versions | `GET` | `/api/v1/skills/{id}/versions` | Public | Version history |
| Get version | `GET` | `/api/v1/skills/{id}/versions/{version}` | Public | Specific version detail |
| Publish version | `POST` | `/api/v1/skills/{id}/versions/{version}/publish` | Owner/Admin | Mark version as published |
| Unpublish version | `POST` | `/api/v1/skills/{id}/versions/{version}/unpublish` | Owner/Admin | Revoke published status |
| Login | `POST` | `/api/v1/auth/login` | Public | Authenticate, return token |
| Me | `GET` | `/api/v1/auth/me` | Authenticated | Current user info |
| List users | `GET` | `/api/v1/users` | Admin | Admin user management |
| Create user | `POST` | `/api/v1/users` | Admin | Admin-only user creation |

### Design Decisions

- **Version is a sub-resource of skill**, not a standalone entity. This reflects the domain: versions belong to skills.
- **Search is a query parameter on the list endpoint**, not a separate route. This keeps the API surface small.
- **No PATCH.** PUT replaces the full resource. This simplifies the MVP; partial updates can be added later.
- **Pagination** uses `limit`/`offset` query parameters with `Link` headers.

### Router Structure

```python
# app/api/router.py
api_router = APIRouter(prefix="/api/v1")
api_router.include_router(skills.router, prefix="/skills", tags=["skills"])
api_router.include_router(versions.router, prefix="/skills", tags=["versions"])
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(users.router, prefix="/users", tags=["users"])
```

Each router file is thin — it parses the request, calls the service layer, and formats the response.

---

## 5. MCP Interface

### What MCP Means Here

The MCP (Model Context Protocol) interface exposes Alexandria's registry operations as **tools** and **resources** that AI agents can discover and invoke. The MCP server runs as an SSE endpoint within the same FastAPI process.

### Tool vs Resource Semantics

| Operation | MCP Type | Rationale |
|---|---|---|
| List skills | **Resource** (`/skills`) | Read-only data retrieval. Resources are for data that agents query. |
| Get skill | **Resource** (`/skills/{id}`) | Same rationale. |
| Search skills | **Tool** (`search_skills`) | Search involves parameterized queries and filtering logic that fits a tool's request/response model. |
| Register skill | **Tool** (`register_skill`) | Write operation with validation side effects. |
| Update skill | **Tool** (`update_skill`) | Write operation. |
| Get version | **Resource** (`/skills/{id}/versions/{version}`) | Read-only. |
| Publish version | **Tool** (`publish_version`) | State-changing operation. |

### MCP Server Implementation

```python
# app/mcp/server.py

from mcp.server import Server
from mcp.types import Tool, Resource

server = Server("alexandria")

@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="search_skills",
            description="Search for skills in the registry",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "tag": {"type": "string"},
                    "limit": {"type": "integer", "default": 10},
                },
            },
        ),
        Tool(
            name="register_skill",
            description="Register a new skill in the registry",
            inputSchema={...},
        ),
        # ...
    ]

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[Content]:
    # Delegate to the SAME service layer used by REST
    if name == "search_skills":
        result = await skill_service.search_skills(**arguments)
        return [TextContent(text=result.json())]
    ...
```

### Key Design Rule

The MCP handler **imports and calls the same service layer** as the REST routes. It does not duplicate queries, validation, or business logic. This is enforced architecturally and verified by tests.

### Transport

MCP runs over **SSE (Server-Sent Events)** transport, served as an additional route within the FastAPI application:

```
POST /mcp/messages  (client → server)
GET  /mcp/sse       (server → client, SSE stream)
```

This allows the MCP interface to coexist with the REST API on the same port without a separate process.

---

## 6. Database Architecture

### ORM Choice

**SQLAlchemy 2.0+ (async)** with `aiosqlite` for SQLite and `asyncpg` for PostgreSQL.

Why SQLAlchemy:
- Mature, well-documented, standard in the Python ecosystem
- Async support is production-ready
- Alembic migration integration is seamless
- Schema-level features (unique constraints, indexes) work across SQLite and PostgreSQL

### Migration Strategy

**Alembic** for schema migrations.

- Migrations live in `migrations/versions/`
- Alembic is configured to auto-detect the database URL from `config.yml`
- SQLite: Alembic creates the file and applies migrations in-process
- PostgreSQL: Alembic connects to the running database

### ID Strategy

**UUIDs (v4)** as primary keys for all domain entities.

Rationale:
- No ID collision risk across environments
- Safe to expose in URLs (no sequential enumeration)
- Works natively in both SQLite (via `uuid` Python type) and PostgreSQL (`uuid` column type)
- Slight storage overhead is negligible for a registry-scale system

SQLite stores UUIDs as `CHAR(36)` text. PostgreSQL uses native `uuid` type. SQLAlchemy handles the abstraction.

### Timestamps

All entities get:
- `created_at` — set once on insert, never changed
- `updated_at` — set on insert, updated on every modification

Both are stored as UTC. The application layer handles timezone conversion if needed.

### Indexes

```sql
-- Critical for MVP
CREATE INDEX idx_skill_name ON skills(name);
CREATE INDEX idx_skill_status ON skills(status);
CREATE INDEX idx_skill_owner ON skills(owner_id);
CREATE INDEX idx_skill_version_skill ON skill_versions(skill_id);
CREATE INDEX idx_skill_version_version ON skill_versions(skill_id, version);
CREATE INDEX idx_skill_version_status ON skill_versions(status);
CREATE INDEX idx_skill_version_tags ON skill_versions(tags);  -- GIN index on PostgreSQL, partial index on SQLite
```

### Uniqueness Constraints

```sql
ALTER TABLE skills ADD CONSTRAINT uq_skill_name UNIQUE(name);
ALTER TABLE skill_versions ADD CONSTRAINT uq_skill_version UNIQUE(skill_id, version);
```

### SQLite vs PostgreSQL Differences

| Concern | SQLite | PostgreSQL |
|---|---|---|
| Concurrent writes | File-level locking; fine for single-process dev | Full MVCC; handles concurrent access |
| JSON queries | Limited JSON support; sufficient for MVP queries | Full `jsonb` with indexing |
| Array columns | Stored as JSON text | Native `text[]` with GIN index |
| Connection pool | Single `aiosqlite` connection | `asyncpg` pool with configurable size |
| Migrations | Identical Alembic scripts | Identical Alembic scripts |

### How the Switch Works

1. Change `config.yml`: `database.driver: postgresql`
2. Set environment variable: `DATABASE_URL=postgresql+asyncpg://...`
3. Run `alembic upgrade head` against PostgreSQL
4. Done. No application code changes.

The repository layer uses SQLAlchemy's dialect-agnostic query API. No raw SQL in application code.

---

## 7. Authentication Architecture

### Provider Abstraction

```python
# app/auth/provider.py

class AuthProvider(Protocol):
    async def authenticate(self, credentials: Credentials) -> AuthResult: ...
    async def get_user(self, token: str) -> User | None: ...
    async def create_token(self, user: User) -> str: ...
```

Two implementations:
- `LocalAuthProvider` — validates against Alexandria's own `users` table, issues JWTs
- `IdentityServerProvider` — delegates to an external IdentityServer

The active provider is selected by `config.yml`.

### MVP: Local Authentication

```
config.yml:
  auth:
    provider: local
    signup: disabled
```

**Flow:**
1. Admin creates a user via the admin UI or API (`POST /api/v1/users`)
2. User logs in with username/password (`POST /api/v1/auth/login`)
3. System returns a JWT
4. JWT is sent as `Authorization: Bearer <token>` on subsequent requests

**Password Storage:** Bcrypt (via `passlib`). Cost factor 12.

**Token Strategy:** JWT with short expiry (1 hour). No refresh token in MVP — the admin UI handles re-login. Refresh tokens are a later addition.

### Authorization (MVP)

A single role field on the `User` model:

| Role | Permissions |
|---|---|
| `admin` | All operations: manage users, all skill CRUD, system config |
| `publisher` | Create/update/delete own skills, publish/unpublish own versions |

No fine-grained RBAC. No resource-level permission checks beyond ownership.

**Ownership rule:** A `publisher` can only modify skills where `skill.owner_id == user.id`. Admins can modify any skill.

### Later: IdentityServer

```yaml
auth:
  provider: identityserver
  issuer: https://identity.example.com
  client_id: alexandria
  client_secret: ${IDENTITYSERVER_CLIENT_SECRET}  # from env
  signup: enabled  # allow self-service registration via IdP
```

The `IdentityServerProvider` validates JWTs issued by the external IdP and maps claims to local user records. This is architecturally clean because the service layer never knows which provider is active.

### Signup Policy

| Mode | Behavior |
|---|---|
| `signup: disabled` | Only admins can create users. No self-registration endpoint. |
| `signup: enabled` | Public `POST /api/v1/auth/signup` endpoint is available. New users get `publisher` role by default. |

MVP ships with `signup: disabled`. The endpoint code exists but is gated by config.

### MCP Access Control

MCP connections authenticate the same way as REST:
- SSE handshake includes the JWT as a query parameter or header
- The MCP server validates the token and attaches the user context
- Tool calls that require auth (register, update, publish) check permissions

Read-only MCP operations (list, search, get) are public — no auth required.

---

## 8. Configuration Model

### Configuration File: `config.yml`

```yaml
# Alexandria Configuration

server:
  host: "0.0.0.0"
  port: 8000
  workers: 1  # Number of uvicorn workers
  reload: true  # Auto-reload in development

database:
  driver: sqlite  # sqlite | postgresql
  # SQLite: path relative to project root
  path: "data/alexandria.db"
  # PostgreSQL: full URL (or use DATABASE_URL env var)
  # url: "postgresql+asyncpg://user:pass@localhost:5432/alexandria"
  pool_size: 5  # PostgreSQL only

auth:
  provider: local  # local | identityserver
  signup: disabled  # disabled | enabled
  jwt_secret: ${JWT_SECRET}  # Required, from env var
  jwt_algorithm: "HS256"
  jwt_expiry_minutes: 60

registry:
  # Future: default visibility, moderation settings, etc.
  default_skill_status: draft  # draft | published
```

### Secrets: Environment Variables

Secrets are **never** committed to `config.yml`. They are read from environment variables using `${VAR_NAME}` syntax or Pydantic's `env_settings`.

| Secret | Source |
|---|---|
| `JWT_SECRET` | Environment variable, required |
| `DATABASE_URL` | Environment variable, optional (overrides `config.yml`) |
| `IDENTITYSERVER_CLIENT_SECRET` | Environment variable, only if using IdentityServer |

### Local Development

```bash
# .env (git-ignored)
JWT_SECRET=dev-secret-do-not-use-in-production
DATABASE_URL=sqlite:///data/alexandria.db
```

A `.env.example` file is committed with placeholder values.

### Production Overrides

Environment variables override `config.yml`. The precedence order:

1. Environment variables (highest priority)
2. `config.yml`
3. Defaults in code (lowest priority)

### Config Loading

```python
# app/config/settings.py

from pydantic import BaseSettings
from pydantic_yaml import YamlBaseSettings

class Settings(YamlBaseSettings):
    # Pydantic validates types and provides defaults
    ...
```

Pydantic validates the configuration at startup. Invalid configuration fails immediately with clear error messages.

---

## 9. Admin Frontend

### Technology

**Svelte** (SvelteKit or standalone Svelte build with Vite).

### Scope

The admin frontend is a lightweight tool for administrators and publishers. It is not a public-facing product.

### Pages

| Route | Description | Auth Required |
|---|---|---|
| `/admin/login` | Login form | No |
| `/admin/dashboard` | Overview: skill count, recent activity | Yes |
| `/admin/skills` | Skill list with search/filter | Yes |
| `/admin/skills/new` | Create skill form | Publisher+ |
| `/admin/skills/:id` | Skill detail, version list | Yes |
| `/admin/skills/:id/edit` | Edit skill metadata | Owner/Admin |
| `/admin/skills/:id/versions/:version` | Version detail, publish/unpublish | Owner/Admin |
| `/admin/users` | User management (admin only) | Admin |

### Design Principles

- **Server-driven data, minimal client state.** Each page fetches data from the REST API and renders it. No complex client-side state management.
- **Forms for CRUD.** No drag-and-drop builders, no visual editors.
- **Functional over beautiful.** Standard CSS, no design system.
- **API-first.** The admin frontend is a consumer of the REST API, not a special case.

### Build Output

The Svelte app builds to static files in `admin/dist/`. FastAPI serves these files under `/admin/*`. No separate static file server in development or production.

---

## 10. Public Landing Page

### Purpose

A single HTML page explaining what Alexandria is.

### Content

- Title and tagline: *"Alexandria — A Registry of AI Agent Skills"*
- Brief description of the problem and solution
- How agents discover capabilities via MCP
- Link to the public registry
- Link to documentation / GitHub

### Implementation

**Static HTML + minimal CSS.** No JavaScript framework. No build step.

Served from `web/public/index.html` at the root path `/`.

---

## 11. Public Registry

### Purpose

A searchable, browsable catalog of published skills. This is the public face of Alexandria.

### Implementation

**Server-rendered HTML or minimal static HTML with client-side fetch.**

Two viable approaches:

**Approach A: Server-rendered (Recommended MVP)**

```python
# app/api/pages.py

@router.get("/registry", response_class=HTMLResponse)
async def registry_page(request: Request):
    skills = await skill_service.list_skills(status="published")
    return templates.TemplateResponse("registry.html", {
        "request": request,
        "skills": skills,
    })
```

FastAPI renders the HTML. The page includes a search form that submits as a standard HTTP GET. No JavaScript required for basic browsing.

**Approach B: Static HTML + client-side fetch (Later)**

A Svelte or vanilla-JS page that fetches from `/api/v1/skills` and renders client-side. Better UX for search/filter, but adds complexity.

### Pages

| Route | Description | Auth |
|---|---|---|
| `/` | Landing page | No |
| `/registry` | Browsable catalog of published skills | No |
| `/registry?q=...` | Search results | No |
| `/registry/skills/:id` | Skill detail page (public view) | No |

### Design

- Simple list/table of published skills
- Click to see version details, capabilities, and connection information
- Search by name, tag, or description
- No pagination complexity initially — show all published skills (the registry is small at launch)

---

## 12. Repository/Project Structure

```
alexandria/
├── app/
│   ├── __init__.py
│   ├── main.py                 # FastAPI app factory, startup/shutdown
│   │
│   ├── config/
│   │   ├── __init__.py
│   │   └── settings.py         # Pydantic settings, config.yml loader
│   │
│   ├── models/
│   │   ├── __init__.py
│   │   ├── skill.py            # Skill, SkillVersion SQLAlchemy models
│   │   ├── user.py             # User SQLAlchemy model
│   │   └── schemas.py          # Pydantic request/response schemas
│   │
│   ├── services/
│   │   ├── __init__.py
│   │   ├── skill_service.py    # Skill CRUD use cases
│   │   ├── version_service.py  # Version management use cases
│   │   └── auth_service.py     # User management, authentication
│   │
│   ├── repositories/
│   │   ├── __init__.py
│   │   ├── skill_repo.py       # Skill DB queries
│   │   ├── version_repo.py     # Version DB queries
│   │   └── user_repo.py        # User DB queries
│   │
│   ├── api/
│   │   ├── __init__.py
│   │   ├── router.py           # Main API router, includes sub-routers
│   │   ├── deps.py             # Dependency injection (get_db, get_current_user)
│   │   ├── skills.py           # Skill REST endpoints
│   │   ├── versions.py         # Version REST endpoints
│   │   ├── auth.py             # Login, signup endpoints
│   │   └── users.py            # User management endpoints
│   │
│   ├── mcp/
│   │   ├── __init__.py
│   │   ├── server.py           # MCP server setup, tool/resource registration
│   │   ├── tools.py            # MCP tool handlers (call into services)
│   │   └── resources.py        # MCP resource handlers (call into services)
│   │
│   ├── auth/
│   │   ├── __init__.py
│   │   ├── provider.py         # AuthProvider protocol
│   │   ├── local.py            # LocalAuthProvider implementation
│   │   ├── identityserver.py   # IdentityServerProvider (stubbed for MVP)
│   │   └── password.py         # Bcrypt password hashing
│   │
│   └── db/
│       ├── __init__.py
│       ├── engine.py           # Async engine factory (SQLite/PostgreSQL)
│       ├── session.py          # Session factory
│       └── base.py             # SQLAlchemy declarative base
│
├── admin/
│   ├── src/                    # Svelte source files
│   │   ├── App.svelte
│   │   ├── routes/
│   │   │   ├── Login.svelte
│   │   │   ├── Dashboard.svelte
│   │   │   ├── SkillList.svelte
│   │   │   ├── SkillDetail.svelte
│   │   │   ├── SkillForm.svelte
│   │   │   └── UserList.svelte
│   │   ├── lib/
│   │   │   ├── api.js          # REST API client
│   │   │   └── auth.js         # Auth token management
│   │   └── main.js
│   ├── package.json
│   ├── vite.config.js
│   └── dist/                   # Build output (served by FastAPI)
│
├── web/
│   ├── templates/
│   │   ├── base.html           # Base template
│   │   ├── index.html          # Landing page
│   │   ├── registry.html       # Public skill catalog
│   │   └── skill_detail.html   # Public skill detail page
│   └── static/
│       └── style.css           # Minimal shared styles
│
├── migrations/
│   ├── env.py                  # Alembic environment config
│   ├── script.py.mako
│   └── versions/
│       └── 001_initial.py      # Initial schema migration
│
├── tests/
│   ├── conftest.py             # Shared fixtures (test DB, test client)
│   ├── test_services/
│   │   ├── test_skill_service.py
│   │   ├── test_version_service.py
│   │   └── test_auth_service.py
│   ├── test_api/
│   │   ├── test_skills.py
│   │   ├── test_versions.py
│   │   ├── test_auth.py
│   │   └── test_users.py
│   ├── test_mcp/
│   │   ├── test_mcp_tools.py
│   │   └── test_mcp_resources.py
│   └── test_integration/
│       └── test_rest_mcp_equivalence.py  # Verify REST and MCP produce same results
│
├── config.yml                 # Default configuration
├── .env.example               # Template for environment variables
├── .gitignore
├── pyproject.toml             # Python project config (dependencies, scripts)
├── README.md
└── ARCHITECTURE.md            # This document
```

### Responsibility Summary

| Module | Responsibility |
|---|---|
| `app/main.py` | FastAPI app creation, lifespan events, mount static files |
| `app/config/` | Load and validate configuration from YAML + env vars |
| `app/models/` | SQLAlchemy models (DB schema) + Pydantic schemas (API contract) |
| `app/services/` | Business logic, use cases. No HTTP, no DB queries directly. |
| `app/repositories/` | Thin database query functions. Called by services. |
| `app/api/` | REST routers. Parse HTTP requests, call services, format responses. |
| `app/mcp/` | MCP server. Parse MCP requests, call services, format MCP responses. |
| `app/auth/` | Authentication providers, password hashing, token management. |
| `app/db/` | Database engine, session factory, declarative base. |
| `admin/` | Svelte admin frontend. |
| `web/` | Public HTML pages and templates. |
| `migrations/` | Alembic database migrations. |
| `tests/` | Test suite. |

---

## 13. Security Model

### Password Storage

- **Bcrypt** via `passlib` with cost factor 12
- Passwords stored only in `users.password_hash`
- Plaintext passwords are never logged, stored, or returned in API responses
- API responses never include `password_hash`

### Token Strategy

- **JWT** tokens issued by local auth provider
- **Short expiry:** 60 minutes (configurable)
- **No refresh tokens in MVP** — admin UI handles re-login
- **Signing:** HS256 with a secret from `JWT_SECRET` environment variable
- Token payload: `{"sub": user_id, "role": role, "exp": timestamp}`

### API Authentication

| Endpoint | Auth Required |
|---|---|
| `GET /api/v1/skills` | No |
| `GET /api/v1/skills/{id}` | No |
| `POST /api/v1/auth/login` | No |
| `POST /api/v1/auth/signup` | No (but gated by config) |
| All other `POST/PUT/DELETE` | Yes (`Authorization: Bearer <token>`) |
| `POST /api/v1/users` | Admin only |

### MCP Access Control

- MCP SSE connection requires valid JWT (passed as query param or header)
- Read operations (list, search, get) are public even via MCP
- Write operations (register, update, publish) require authentication
- Publish/unpublish require owner or admin

### Admin Authorization

Simple ownership check:

```python
if user.role != "admin" and skill.owner_id != user.id:
    raise ForbiddenError("You can only modify your own skills")
```

No RBAC matrix. No permission table. One role field, one ownership check.

### Signup Policy

- `signup: disabled` → No public registration. Admins create accounts manually.
- `signup: enabled` → `POST /api/v1/auth/signup` is available. New users get `publisher` role.

### Secrets Management

- `JWT_SECRET`: Environment variable, required. App refuses to start without it.
- Database credentials: Environment variable (`DATABASE_URL`).
- `.env` file is git-ignored.
- `.env.example` is committed with placeholder values.
- `config.yml` contains no secrets in the committed version.

### Auditability (MVP)

Minimal audit trail:
- `skills.created_by`, `skills.updated_by` (FK → User)
- `skill_versions.published_by` (FK → User)
- `users.created_by` (FK → User)

No separate audit log table in MVP. The `created_by` / `updated_by` fields provide basic traceability.

---

## 14. Testing Strategy

### Test Levels (Priority Order)

#### 1. Service/Use-Case Tests (Highest Priority)

Test business logic in isolation, with a mocked database session.

```python
# tests/test_services/test_skill_service.py

async def test_create_skill(db_session):
    service = SkillService(db_session)
    skill = await service.create_skill(
        owner_id=test_user_id,
        data=SkillCreate(name="web-search", display_name="Web Search", description="Search the web"),
    )
    assert skill.name == "web-search"
    assert skill.status == SkillStatus.DRAFT
```

**Why highest priority:** The service layer IS the system. If the service layer is correct, both REST and MCP will be correct. Tests here catch business logic bugs directly.

#### 2. REST API Tests

Test HTTP endpoints end-to-end, using a real test database (SQLite in-memory).

```python
# tests/test_api/test_skills.py

async def test_list_skills(client, published_skill):
    response = await client.get("/api/v1/skills")
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
```

#### 3. MCP Interface Tests

Test MCP tool and resource handlers directly, verifying they produce the same results as the service layer.

```python
# tests/test_mcp/test_mcp_tools.py

async def test_search_skills_mcp(db_session, published_skill):
    result = await handle_tool_call("search_skills", {"query": "web"})
    assert result[0].text  # Contains skill data
```

#### 4. REST/MCP Equivalence Tests (Critical)

**This is the most important architectural test.** Verify that REST and MCP produce equivalent results for the same operations.

```python
# tests/test_integration/test_rest_mcp_equivalence.py

async def test_list_skills_equivalence(client, mcp_handler, published_skill):
    # REST call
    rest_response = await client.get("/api/v1/skills")
    rest_skills = rest_response.json()["items"]

    # MCP call (same underlying operation)
    mcp_result = await mcp_handler.list_resources()
    mcp_skills = parse_mcp_resources(mcp_result)

    # They must return the same data
    assert len(rest_skills) == len(mcp_skills)
    assert rest_skills[0]["name"] == mcp_skills[0]["name"]
```

This test catches the dangerous scenario where REST and MCP drift apart in behavior.

#### 5. Authentication Tests

```python
async def test_login_success(client, user):
    response = await client.post("/api/v1/auth/login", json={
        "username": "testuser",
        "password": "correctpassword",
    })
    assert response.status_code == 200
    assert "token" in response.json()

async def test_login_failure(client):
    response = await client.post("/api/v1/auth/login", json={
        "username": "wrong",
        "password": "wrong",
    })
    assert response.status_code == 401

async def test_protected_endpoint_without_token(client):
    response = await client.post("/api/v1/skills", json={})
    assert response.status_code == 401
```

#### 6. Database Integration Tests

Test repository functions against a real SQLite database.

```python
async def test_skill_uniqueness(db_session):
    await create_skill_in_db(db_session, name="web-search")
    with pytest.raises(IntegrityError):
        await create_skill_in_db(db_session, name="web-search")
```

### Test Infrastructure

- **In-memory SQLite** for fast test isolation
- **pytest** as test framework
- **pytest-asyncio** for async test support
- **httpx** with FastAPI's `TestClient` for REST API tests
- **Shared fixtures** in `conftest.py` (test DB session, test client, sample data)

### Coverage Target

MVP: 80%+ on services and repositories. Lower acceptable on API routing and frontend.

---

## 15. Deployment Model

### MVP: Single Process

```
┌─────────────────────────────────────────┐
│             Single Process              │
│                                         │
│  FastAPI (uvicorn)                      │
│  ├── REST API (port 8000)              │
│  ├── MCP SSE endpoint (port 8000)      │
│  ├── Static admin frontend (/admin)    │
│  ├── Public HTML pages (/)             │
│  └── SQLite file (data/alexandria.db)  │
│                                         │
└─────────────────────────────────────────┘
```

**Run locally:**

```bash
# Install dependencies
pip install -e ".[dev]"

# Run database migrations
alembic upgrade head

# Start the server
uvicorn app.main:app --reload
```

**Single command to start everything.** No Docker, no process managers, no external services required.

### Production Evolution

#### Stage 1: SQLite (MVP)

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

SQLite file stored on local disk. Sufficient for single-server deployment with modest concurrent load.

#### Stage 2: PostgreSQL

1. Set `database.driver: postgresql` in `config.yml`
2. Set `DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/alexandria`
3. Run `alembic upgrade head` against PostgreSQL
4. Run the same `uvicorn` command

No application code changes. No Docker required (but Docker Compose is convenient for PostgreSQL).

#### Stage 3: Behind a Reverse Proxy

```
Nginx / Caddy → uvicorn (app) → PostgreSQL
                Static files (admin, public)
```

Standard production Python deployment.

### What We Explicitly Do NOT Need

| Infrastructure | MVP? | Why Not |
|---|---|---|
| Docker | Optional | Nice for dev environment consistency, not required |
| Docker Compose | Optional | Useful when PostgreSQL is introduced |
| Kubernetes | Avoid | Overkill for a single-process app |
| Load balancer | Later | Not needed until scale demands multiple workers |
| Message queue | Avoid | No async processing requirements |
| Redis | Avoid | No caching or session store needed |
| CI/CD | Recommended | GitHub Actions or similar, simple deploy script |

---

## 16. Phased Implementation Roadmap

### Phase 1: Foundation (Weeks 1-2)

**Goal:** Backend skeleton that runs and responds to requests.

| Task | Deliverable |
|---|---|
| Project scaffolding | Directory structure, `pyproject.toml`, dependencies |
| Configuration loading | `config.yml` parsed by Pydantic settings |
| Database setup | SQLAlchemy async engine, session factory, base model |
| Initial migration | Alembic with `skills`, `skill_versions`, `users` tables |
| Domain models | SQLAlchemy models + Pydantic schemas |
| Seed data script | Create admin user + sample skill for dev |

**Milestone:** `uvicorn app.main:app` starts, creates SQLite DB, serves an empty `/api/v1/skills` endpoint.

### Phase 2: Core Services (Week 3)

**Goal:** Service layer implements all use cases.

| Task | Deliverable |
|---|---|
| Repository layer | DB query functions for skills, versions, users |
| SkillService | CRUD operations for skills |
| VersionService | Version management, publish/unpublish |
| AuthService | User creation, authentication, JWT generation |
| Service tests | Unit tests for all service methods |

**Milestone:** All business logic implemented and tested independently of HTTP/MCP.

### Phase 3: REST API (Week 4)

**Goal:** Full REST API surface.

| Task | Deliverable |
|---|---|
| REST routers | All endpoints from the endpoint matrix |
| Auth middleware | JWT validation, role-based permission checks |
| Error handling | Domain exception → HTTP status code mapping |
| API tests | End-to-end tests for all endpoints |

**Milestone:** Complete REST API, all tests passing, browsable via FastAPI's auto-generated docs at `/docs`.

### Phase 4: MCP Interface (Week 5)

**Goal:** MCP server exposing the same capabilities.

| Task | Deliverable |
|---|---|
| MCP server setup | SSE transport within FastAPI |
| MCP tools | `search_skills`, `register_skill`, `update_skill`, `publish_version` |
| MCP resources | `/skills`, `/skills/{id}`, `/skills/{id}/versions/{version}` |
| MCP tests | Tool/resource handler tests |
| Equivalence tests | REST vs MCP behavioral parity |

**Milestone:** An MCP client (e.g., Claude Desktop) can discover, search, and register skills through the MCP interface.

### Phase 5: Authentication + Admin UI (Weeks 6-7)

**Goal:** Functional authentication and admin interface.

| Task | Deliverable |
|---|---|
| Local auth provider | JWT-based auth with bcrypt passwords |
| Signup toggle | Config-gated self-registration |
| Svelte admin app | Login, dashboard, skill CRUD, user management |
| Admin integration | FastAPI serves built Svelte app at `/admin` |

**Milestone:** Admin can log in, create skills, manage versions, and manage users through the browser.

### Phase 6: Public Site (Week 8)

**Goal:** Public-facing registry and landing page.

| Task | Deliverable |
|---|---|
| Landing page | Static HTML explaining Alexandria |
| Public registry | Server-rendered catalog of published skills |
| Skill detail page | Public view of a skill's metadata and capabilities |

**Milestone:** Anyone can browse the registry at `https://alexandria.example.com/registry`.

### Phase 7: Polish and Hardening (Week 9)

**Goal:** Production-readiness.

| Task | Deliverable |
|---|---|
| PostgreSQL testing | Verify all migrations and queries work with PostgreSQL |
| Error handling | Consistent error responses across all interfaces |
| Logging | Structured logging for all operations |
| CORS configuration | If needed for cross-origin admin frontend |
| Documentation | API docs, deployment guide |

### Phase 8: Future (Post-MVP)

| Feature | When |
|---|---|
| PostgreSQL as primary DB | When concurrent access or scale requires it |
| IdentityServer integration | When organizational SSO is needed |
| Open signup | When community participation is desired |
| Skill dependencies | When skills need to reference other skills |
| Usage analytics | When publishers need consumption data |
| API keys | When programmatic access without user login is needed |
| Webhook notifications | When subscribers need skill change alerts |

---

## 17. MVP vs Later-Feature Matrix

| Feature | MVP | Later | Only If Required | Not Recommended |
|---|---|---|---|---|
| SQLite database | ✅ | | | |
| PostgreSQL support | | ✅ | | |
| Local authentication | ✅ | | | |
| IdentityServer | | | ✅ | |
| JWT tokens | ✅ | | | |
| Refresh tokens | | ✅ | | |
| Admin role | ✅ | | | |
| Publisher role | ✅ | | | |
| Fine-grained RBAC | | | ✅ | |
| REST API | ✅ | | | |
| MCP interface | ✅ | | | |
| MCP resources | ✅ | | | |
| MCP prompts | | ✅ | | |
| Skill versioning | ✅ | | | |
| Skill dependencies | | ✅ | | |
| Skill capabilities (JSON) | ✅ | | | |
| Skill capabilities (table) | | ✅ | | |
| Tags/search | ✅ | | | |
| Full-text search | | ✅ | | |
| Vector search | | | | ✅ |
| Svelte admin UI | ✅ | | | |
| Public landing page | ✅ | | | |
| Public registry | ✅ | | | |
| Self-service signup | | ✅ | | |
| User profile pages | | ✅ | | |
| Multi-tenancy | | | | ✅ |
| Microservices | | | | ✅ |
| Message broker | | | | ✅ |
| Redis | | | | ✅ |
| Event sourcing | | | | ✅ |
| CQRS | | | | ✅ |
| Kubernetes | | | | ✅ |
| Docker Compose | | ✅ | | |
| CI/CD pipeline | | ✅ | | |
| Rate limiting | | ✅ | | |
| API key auth | | ✅ | | |
| Webhook notifications | | ✅ | | |
| Usage analytics | | ✅ | | |
| Audit log table | | ✅ | | |
| Structured logging | ✅ | | | |
| Distributed tracing | | | | ✅ |
| Plugin marketplace | | | | ✅ |
| Workflow engine | | | | ✅ |
| Organization/team model | | ✅ | | |
| Skill reviews/ratings | | ✅ | | |

---

## 18. Key Architectural Decisions and Trade-offs

### Decision 1: Single FastAPI Process

**Choice:** One uvicorn process serves REST, MCP, admin UI, and public site.

**Trade-off:** Simplicity over separation. Each interface cannot be independently scaled or deployed.

**Why:** For the MVP, this eliminates an entire class of infrastructure complexity (inter-service communication, separate deployments, load balancing). The application is a registry — it does not have the performance profile that justifies splitting into separate services.

**When to revisit:** When the registry serves hundreds of concurrent agents and needs independent scaling, or when the public site has vastly different performance characteristics than the admin API.

### Decision 2: JSON for Capabilities

**Choice:** Capabilities are stored as a JSON column on `SkillVersion`, not as a separate table.

**Trade-off:** Query flexibility over normalization. You cannot efficiently query "all skills that have a `web_search` capability" with a JSON column.

**Why:** The registry's primary query path is "get skill → get version → read capabilities." Independent capability queries are not yet a requirement. A JSON column is zero-migration to add later if needed.

**When to revisit:** When cross-skill capability queries become a requirement (e.g., "find all skills that expose a `code_review` tool").

### Decision 3: Local Auth First

**Choice:** JWT-based local authentication with admin-created users.

**Trade-off:** Simplicity over integration. Users have a separate set of credentials for Alexandria.

**Why:** Local auth requires zero external dependencies. The auth provider abstraction means IdentityServer can be added later without changing the service layer.

**When to revisit:** When the organization has an existing identity provider and managing separate credentials is a burden.

### Decision 4: Server-Rendered Public Registry

**Choice:** FastAPI templates render the public registry HTML server-side.

**Trade-off:** Server load per request over client-side JavaScript complexity. Every page load hits the server.

**Why:** The registry is small (likely hundreds of skills, not millions). Server rendering is simpler, faster to implement, and better for initial SEO. Client-side rendering can be introduced later for interactive search/filter.

**When to revisit:** When the public registry needs rich interactive features (live search, filters, sorting) that justify a JavaScript framework.

### Decision 5: Alembic for Migrations

**Choice:** Alembic manages database schema changes.

**Trade-off:** Slight setup complexity over "just create the tables."

**Why:** Alembic provides version-controlled, repeatable schema changes. Even in the MVP, migrations ensure that database setup is deterministic and reproducible.

**When to revisit:** Never. This is a standard, correct choice.

### Decision 6: UUID Primary Keys

**Choice:** UUIDs (v4) for all primary keys.

**Trade-off:** Slightly larger storage and less human-readable IDs over global uniqueness and safety.

**Why:** UUIDs eliminate sequential ID enumeration, work across environments, and are the standard for systems that may eventually be distributed.

**When to revisit:** Never.

---

## 19. Risks and Failure Modes

### Risk 1: REST/MCP Behavioral Drift

**Description:** Over time, the REST and MCP interfaces implement different behavior for the same operation. This is the most dangerous architectural failure mode.

**Mitigation:**
- The equivalence test suite (`test_rest_mcp_equivalence.py`) runs in CI.
- All business logic lives in the service layer; REST and MCP are thin adapters.
- Code review enforces that no business logic is added directly to REST routes or MCP handlers.

**Detection:** Automated tests. If they pass, the interfaces are equivalent.

### Risk 2: MCP Protocol Evolution

**Description:** The MCP protocol specification may change in ways that break the MCP server implementation.

**Mitigation:**
- Pin the `mcp` library version in `pyproject.toml`.
- The MCP interface is isolated in `app/mcp/`, making updates localized.
- The service layer is completely independent of MCP.

**Detection:** MCP client connection failures.

### Risk 3: SQLite Limitations in Production

**Description:** SQLite's file-level locking becomes a bottleneck under concurrent write load.

**Mitigation:**
- The database abstraction makes PostgreSQL a configuration change, not a rewrite.
- The README documents PostgreSQL as the recommended production database.
- The service layer uses SQLAlchemy's dialect-agnostic query API.

**Detection:** Increased write latency, `SQLITE_BUSY` errors.

### Risk 4: JWT Secret Compromise

**Description:** The `JWT_SECRET` is leaked, allowing token forgery.

**Mitigation:**
- Secret is never in `config.yml` or committed files.
- Secret is loaded from environment variables.
- Short token expiry (60 minutes) limits the window of compromise.
- Token revocation can be added later (token deny-list).

**Detection:** Unauthorized access with valid-looking tokens.

### Risk 5: Premature Complexity Creep

**Description:** Features are added to the MVP that increase scope without proportional value.

**Mitigation:**
- The feature matrix (Section 17) explicitly classifies what belongs in MVP vs later.
- The architecture plan is the reference document; deviations require explicit justification.
- "Later" items have a documented trigger for when they become relevant.

**Detection:** Implementation time exceeds estimates by >50%.

### Risk 6: Skill Name Namespace Conflicts

**Description:** Two publishers register skills with the same name.

**Mitigation:**
- The `name` field is unique in the database.
- A conflict returns a clear error.
- Future evolution: namespace by owner (`org/skill-name`).

**Detection:** Database constraint violation on insert.

---

## 20. Recommended First Milestone

### Milestone 0: "Hello Registry"

**Time estimate:** 2-3 days

**Objective:** A running FastAPI server with SQLite, a single REST endpoint, and a health check. Proves the toolchain works.

**Deliverables:**
- Project structure created
- `pyproject.toml` with all dependencies
- `config.yml` + settings loading
- SQLAlchemy engine + session factory
- Initial Alembic migration (creates `skills` table only)
- `GET /api/v1/health` returns `{"status": "ok"}`
- `GET /api/v1/skills` returns `[]`
- Server starts with `uvicorn app.main:app --reload`
- `.env.example` documented

**Validation criteria:**
```bash
# Server starts
uvicorn app.main:app --reload

# Health check
curl http://localhost:8000/api/v1/health
# → {"status": "ok"}

# Empty skill list
curl http://localhost:8000/api/v1/skills
# → {"items": [], "total": 0, "limit": 20, "offset": 0}

# SQLite DB exists
ls data/alexandria.db
# → exists and contains skills table
```

### After Milestone 0, proceed in order:

1. **Phase 1 completion** — Full domain model, all migrations, seed data
2. **Phase 2** — Service layer with tests
3. **Phase 3** — REST API
4. **Phase 4** — MCP interface
5. **Phase 5** — Auth + Admin UI
6. **Phase 6** — Public site
7. **Phase 7** — Polish

Each phase produces a testable, shippable increment. No phase depends on all phases being complete.

---

## Summary

Alexandria's architecture is defined by one principle: **one service layer, two interfaces**. The MVP is a single FastAPI process with SQLite, local auth, a REST API, an MCP server, a Svelte admin UI, and a static public registry. It has no message broker, no Redis, no Kubernetes, no microservices, and no event bus.

The system is designed to grow by configuration changes (SQLite → PostgreSQL), provider swaps (local → IdentityServer), and additive features (new MCP tools, new API endpoints) — not by rearchitecting its core.

The smallest useful Alexandria is a registry you can `pip install` and `uvicorn` in under five minutes. Everything else is optional.
