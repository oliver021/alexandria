# Debts

Problemas detectados al revisar el despliegue de Alexandria (revisión de código y `DEPLOY.md`; no se ejecutó la app).

## Seguridad

### 1. Credenciales fijas del admin
- **Dónde:** `scripts/seed.py`
- **Problema:** crea siempre `admin` / `admin123456`.
- **Acción:** leer usuario y contraseña desde variables de entorno, o generar una contraseña aleatoria y mostrarla una vez.

### 2. `JWT_SECRET` sin validar
- **Dónde:** `app/config/settings.py` (`AuthSettings`)
- **Problema:** si la variable no está definida, el secreto queda vacío y la app arranca igual, firmando tokens con una clave vacía. `.env.example` trae `dev-secret-do-not-use-in-production`.
- **Acción:** fallar al arrancar si `JWT_SECRET` está vacío (o es el valor de ejemplo) fuera de desarrollo.

## Base de datos

### 3. `create_all` junto con Alembic
- **Dónde:** `app/db/engine.py` (`init_db`), `migrations/`
- **Problema:** `init_db()` ejecuta `Base.metadata.create_all` al arrancar, además de las migraciones de Alembic. En SQLite las tablas se crean solas y se saltan las migraciones. En PostgreSQL puede haber conflicto con Alembic.
- **Acción:** usar solo Alembic fuera de desarrollo, o dejar `create_all` solo para SQLite/tests.

### 4. `asyncpg` no está en las dependencias
- **Dónde:** `pyproject.toml`
- **Problema:** `DEPLOY.md` usa `postgresql+asyncpg://...`, pero `asyncpg` no está declarado.
- **Acción:** añadirlo como extra opcional (por ejemplo `[postgres]`) y documentarlo.

### 5. `driver: postgresql` en `config.yml` es inútil
- **Dónde:** `app/config/settings.py` (`DatabaseSettings.url`)
- **Problema:** con ese driver apunta a `postgresql+asyncpg://localhost:5432/alexandria`, sin credenciales.
- **Acción:** exigir `DATABASE_URL` cuando el driver sea PostgreSQL, o eliminar la rama.

## Configuración

### 6. `reload: true` por defecto
- **Dónde:** `config.yml`
- **Problema:** solo afecta a `run()` (`python -m app.main` / comando `alexandria`). Con el comando de uvicorn de producción no aplica, pero es un valor peligroso por defecto.
- **Acción:** ponerlo en `false` y activarlo solo en desarrollo.

### 7. CORS solo permite `localhost`
- **Dónde:** `config.yml`, `app/config/settings.py`
- **Problema:** los orígenes permitidos son `localhost:8000` y `localhost:5173`. Un dominio real no funcionaría.
- **Acción:** añadir el dominio de producción, idealmente por variable de entorno.

## Infraestructura

### 8. Sin Dockerfile, compose ni CI
- **Problema:** el despliegue es manual (pip, alembic, seed, uvicorn). `ARCHITECTURE.md` los marca como opcionales o recomendados.
- **Acción:** añadir `Dockerfile` (y compose con PostgreSQL), o un servicio systemd, y un pipeline de CI simple.
