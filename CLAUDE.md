# Alexandria

Registro ligero de skills/capacidades basado en MCP. Capa común de descubrimiento y registro para agentes de IA y aplicaciones.

- Backend: Python 3.11+, FastAPI, SQLAlchemy 2.0 async, Alembic, SQLite (PostgreSQL opcional)
- Admin UI: Svelte (`admin/`), servida en `/admin/` desde `admin/dist/`
- Sitio público: plantillas Jinja en `web/templates`, servido en `/registry`
- Un solo proceso; la lógica vive en `app/services/` y REST (`app/api/`) y MCP (`app/mcp/`) son adaptadores finos

Reglas completas para agentes en @AGENTS.md. Arquitectura en `ARCHITECTURE.md`.

## Deudas conocidas

Detalle en @Debts.md. Resumen:

- `scripts/seed.py` crea siempre `admin` / `admin123456`
- `JWT_SECRET` vacío no hace fallar el arranque
- `init_db()` hace `create_all` además de Alembic
- `asyncpg` no está en las dependencias de `pyproject.toml`
- `driver: postgresql` en `config.yml` apunta a localhost sin credenciales
- `reload: true` por defecto en `config.yml`
- CORS solo permite `localhost`
- Sin Dockerfile, compose ni CI

## Desarrollo

```bash
pip install -e ".[dev]"
cp .env.example .env
alembic upgrade head
python scripts/seed.py
uvicorn app.main:app --reload
```

- Servidor: `http://localhost:8000` (docs en `/docs`, admin en `/admin/`, registro en `/registry`)
- Tests: `pytest` (con cobertura: `pytest --cov=app`)
- Lint: `ruff check .`
- Admin UI: `cd admin && npm install && npm run build` (dev: `npm run dev`, puerto 5173)
- Config: variables de entorno (`JWT_SECRET`, `DATABASE_URL`, `LOGGING_LEVEL`) sobre `config.yml`
