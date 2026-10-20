# Alexandria Deployment Guide

## Quick Start (Development)

```bash
# 1. Install dependencies
pip install -e ".[dev]"

# 2. Copy environment config
cp .env.example .env

# 3. Seed the admin user
python scripts/seed.py

# 4. Run the server
uvicorn app.main:app --reload
```

The server starts at `http://localhost:8000`.

- API docs: `http://localhost:8000/docs`
- Admin UI: `http://localhost:8000/admin/`
- Public registry: `http://localhost:8000/registry`

## Environment Variables

All secrets and configuration are set via environment variables (or `.env` file):

| Variable | Description | Default |
|---|---|---|
| `JWT_SECRET` | Secret key for JWT signing | *(required)* |
| `DATABASE_URL` | Database connection string | `sqlite+aiosqlite:///data/alexandria.db` |
| `LOGGING_LEVEL` | Log level: DEBUG, INFO, WARNING, ERROR | `INFO` |

### CORS Configuration (in `config.yml`)

```yaml
cors:
  allowed_origins:
    - "http://localhost:8000"
    - "http://localhost:5173"
  allow_credentials: true
  allow_methods: ["*"]
  allow_headers: ["*"]
```

## Database

### SQLite (default, MVP)

No setup required. The database file is created automatically at `data/alexandria.db`.

### PostgreSQL

1. Set the environment variable:
   ```bash
   export DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/alexandria
   ```

2. Run migrations:
   ```bash
   alembic upgrade head
   ```

3. Seed the admin user:
   ```bash
   python scripts/seed.py
   ```

## Running Migrations

```bash
# Apply all migrations
alembic upgrade head

# Check current migration version
alembic current

# Rollback one migration
alembic downgrade -1
```

## Testing

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=app

# Run specific test file
pytest tests/test_api/test_skills.py -v

# Run Phase 7 tests (logging, CORS, error handling, PG migration)
pytest tests/test_phase7.py -v

# Run PostgreSQL migration tests (requires DATABASE_URL set to postgresql://)
DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/alexandria_test pytest tests/test_phase7.py -v
```

## Production Deployment

```bash
# 1. Install without dev dependencies
pip install .

# 2. Set environment variables
export JWT_SECRET=$(openssl rand -hex 32)
export DATABASE_URL=postgresql+asyncpg://user:password@db-host:5432/alexandria
export LOGGING_LEVEL=INFO

# 3. Run migrations
alembic upgrade head

# 4. Seed admin user
python scripts/seed.py

# 5. Start the server
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
```

### Admin UI (production)

Build the Svelte admin app before deploying:

```bash
cd admin
npm install
npm run build
cd ..
```

The built files in `admin/dist/` are automatically served at `/admin/`.

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for full architecture details.

- **Service layer:** `app/services/` — all business logic
- **REST API:** `app/api/` — thin HTTP adapter
- **MCP interface:** `app/mcp/` — thin MCP adapter
- **Public site:** `app/api/public.py` — server-rendered pages
- **Admin UI:** `admin/` — Svelte SPA served at `/admin/`
