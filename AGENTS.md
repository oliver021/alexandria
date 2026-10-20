# AGENTS.md — Alexandria Project Rules & Indexes

> Minimum rules and file indexes for AI agents working on this codebase.

---

## Project Overview

**Alexandria** is a lightweight skill/capability registry for AI agents, built around MCP (Model Context Protocol). Single FastAPI process, SQLite (MVP), local auth.

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy 2.0+ async, Alembic, aiosqlite, Pydantic, MCP SDK, Svelte (admin), pytest.

---

## Mandatory Rules

### 1. Architecture Constraint
- **One service layer, two interfaces.** All business logic lives in `app/services/`. REST (`app/api/`) and MCP (`app/mcp/`) are thin adapters only.
- **Never add business logic** directly to REST routes or MCP handlers.
- Services receive a DB session, not a repository class.
- Services raise **domain exceptions**, not HTTP exceptions.

### 2. Code Style
- **No comments** unless explicitly requested.
- **No framework dependency in services** — no FastAPI imports in `app/services/`.
- **Uniform pagination** — every list returns `PaginatedResult[T]` with `items`, `total`, `limit`, `offset`.
- Use `async def` everywhere for DB operations.
- UUIDs (v4) as primary keys for all entities.

### 3. Configuration
- Secrets from **environment variables only** — never in `config.yml` or committed files.
- Config loading via Pydantic (`app/config/settings.py`).
- `.env` is git-ignored; `.env.example` is committed.

### 4. Database
- **Alembic** for all schema changes. Never create tables manually.
- SQLite for MVP. PostgreSQL later via config change.
- Migrations in `migrations/versions/`.

### 5. Testing
- **pytest** + **pytest-asyncio** + **httpx**.
- Service tests are highest priority — test business logic in isolation.
- REST/MCP equivalence tests are critical — verify behavioral parity.
- In-memory SQLite for test isolation.
- Run: `pytest` (from project root).

### 6. Git
- Do not commit secrets, `.env`, or `data/`.
- Do not commit without being explicitly asked.
- Conventional commits preferred.

---

## File Index

### Project Root
| File | Purpose |
|---|---|
| `ARCHITECTURE.md` | Full architecture, domain model, roadmap |
| `AGENTS.md` | This file — rules and indexes |
| `DEPLOY.md` | Deployment guide, env vars, migration commands |
| `config.yml` | Application configuration |
| `.env.example` | Environment variable template |
| `pyproject.toml` | Dependencies, scripts, project metadata |
| `.gitignore` | Git ignore rules |
| `README.md` | Project readme |

### `app/` — Application Core
| Path | Purpose |
|---|---|
| `app/main.py` | FastAPI app factory, startup/shutdown, CORS, error handlers, request logging |
| `app/logging.py` | Structured JSON logging setup |
| `app/config/settings.py` | Pydantic settings, config.yml + env var loading (including CORS, logging) |
| `app/db/base.py` | SQLAlchemy declarative base |
| `app/db/engine.py` | Async engine factory (SQLite/PostgreSQL) |
| `app/db/session.py` | Async session factory |
| `app/models/skill.py` | `Skill`, `SkillVersion` SQLAlchemy models |
| `app/models/user.py` | `User` SQLAlchemy model |
| `app/models/schemas.py` | Pydantic request/response schemas |
| `app/services/skill_service.py` | Skill CRUD use cases |
| `app/services/version_service.py` | Version management use cases |
| `app/services/auth_service.py` | User creation, authentication |
| `app/repositories/skill_repo.py` | Skill DB query functions |
| `app/repositories/version_repo.py` | Version DB query functions |
| `app/repositories/user_repo.py` | User DB query functions |
| `app/api/router.py` | Main API router, includes sub-routers |
| `app/api/deps.py` | Dependency injection (get_db, get_current_user) |
| `app/api/public.py` | Public site: landing, registry, skill detail |
| `app/api/skills.py` | Skill REST endpoints |
| `app/api/versions.py` | Version REST endpoints |
| `app/api/auth.py` | Login, signup endpoints |
| `app/api/users.py` | User management endpoints |
| `app/mcp/server.py` | MCP server setup, tool/resource registration |
| `app/mcp/tools.py` | MCP tool handlers (call into services) |
| `app/mcp/resources.py` | MCP resource handlers (call into services) |
| `app/auth/provider.py` | `AuthProvider` protocol |
| `app/auth/local.py` | `LocalAuthProvider` implementation |
| `app/auth/identityserver.py` | `IdentityServerProvider` (stubbed) |
| `app/auth/password.py` | Bcrypt password hashing |

### `admin/` — Svelte Admin Frontend
| Path | Purpose |
|---|---|
| `admin/src/App.svelte` | Root Svelte component |
| `admin/src/routes/*.svelte` | Page components (Login, Dashboard, SkillList, etc.) |
| `admin/src/lib/api.js` | REST API client |
| `admin/src/lib/auth.js` | Auth token management |
| `admin/dist/` | Build output (served by FastAPI at `/admin`) |

### `web/` — Public Site
| Path | Purpose |
|---|---|
| `web/templates/base.html` | Base HTML template |
| `web/templates/index.html` | Landing page |
| `web/templates/registry.html` | Public skill catalog |
| `web/templates/skill_detail.html` | Public skill detail |
| `web/static/style.css` | Shared styles |

### `migrations/` — Alembic
| Path | Purpose |
|---|---|
| `migrations/env.py` | Alembic environment config |
| `migrations/versions/001_initial.py` | Initial schema migration |

### `tests/` — Test Suite
| Path | Purpose |
|---|---|
| `tests/conftest.py` | Shared fixtures (test DB, client, sample data) |
| `tests/test_services/` | Service layer unit tests |
| `tests/test_api/` | REST API end-to-end tests |
| `tests/test_mcp/` | MCP tool/resource handler tests |
| `tests/test_integration/` | REST/MCP equivalence tests |
| `tests/test_phase7.py` | Phase 7 tests: error responses, CORS, logging, PG migration |

---

## Milestone Roadmap

| # | Milestone | Goal | Weeks |
|---|---|---|---|
| 0 | Hello Registry | Project scaffolding, DB, health check, empty skill list | 0.5 |
| 1 | Foundation Complete | Full domain model, all migrations, seed data | 1-2 |
| 2 | Core Services | Repository + service layer with tests | 3 |
| 3 | REST API | All endpoints, auth middleware, error handling | 4 |
| 4 | MCP Interface | MCP server, tools, resources, equivalence tests | 5 |
| 5 | Auth + Admin UI | Local auth, Svelte admin app | 6-7 |
| 6 | Public Site | Landing page, registry, skill detail | 8 |
| 7 | Polish | PostgreSQL testing, logging, error handling, docs | 9 |

---

## Commands

```bash
# Run server
uvicorn app.main:app --reload

# Run migrations
alembic upgrade head

# Run tests
pytest

# Run tests with coverage
pytest --cov=app

# Install dev dependencies
pip install -e ".[dev]"
```
