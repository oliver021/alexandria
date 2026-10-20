# Alexandria

A lightweight skill/capability registry built around MCP, providing a common discovery and registration layer for AI agents and applications.

## Quick Start

```bash
# Install dependencies
pip install -e ".[dev]"

# Set up environment
cp .env.example .env

# Run migrations
alembic upgrade head

# Seed admin user
python scripts/seed.py

# Start server
uvicorn app.main:app --reload
```

## API

- `GET /api/v1/health` — Health check
- `GET /api/v1/skills` — List skills
- `POST /api/v1/skills` — Create skill (auth required)
- `POST /api/v1/auth/login` — Login
- `GET /api/v1/auth/me` — Current user
- Swagger docs at `/docs`

## Configuration

Environment variables override `config.yml`. See `.env.example` for available options.
