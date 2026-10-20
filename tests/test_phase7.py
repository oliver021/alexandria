from __future__ import annotations

import json
import logging
import os

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.logging import JSONFormatter
from app.models.schemas import UserCreate
from app.services.auth_service import AuthService
from app.services.errors import StorageError
from app.services.skill_service import SkillService
from tests.helpers.factories import Actors
from tests.helpers.packages import make_skill_zip
from tests.test_api.support import JWT_SECRET, Api, assert_error, create_tokens


@pytest.fixture(autouse=True)
def phase7_settings(monkeypatch: pytest.MonkeyPatch, fast_password_hashing: None) -> None:
    monkeypatch.setattr(settings.auth, "jwt_secret", JWT_SECRET)
    monkeypatch.setattr(settings.registry, "public_read", True)
    monkeypatch.setattr(settings.registry, "auto_publish", False)


@pytest_asyncio.fixture
async def api(client: AsyncClient, db_session: AsyncSession, actors: Actors) -> Api:
    return Api(client, await create_tokens(db_session, actors))


async def upload(api: Api, name: str = "log-skill", version: str = "1.0.0", **kwargs):
    return await api.upload(make_skill_zip(name, version), "owner", **kwargs)


@pytest.mark.asyncio
async def test_error_response_format_not_found(client: AsyncClient):
    r = await client.get("/api/v1/skills/nonexistent")

    assert r.status_code == 404
    assert r.json() == {"error": {"code": "NOT_FOUND", "message": "Skill not found: 'nonexistent'"}}


@pytest.mark.asyncio
async def test_error_response_format_unauthorized(client: AsyncClient):
    r = await client.get("/api/v1/auth/me")

    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"
    assert r.json()["error"]["code"] == "UNAUTHORIZED"
    assert "detail" not in r.json()


@pytest.mark.asyncio
async def test_error_response_format_conflict(api: Api):
    await upload(api)

    r = await upload(api)

    assert r.status_code == 409
    body = r.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == "CONFLICT"
    assert "already exists" in body["error"]["message"]


@pytest.mark.asyncio
async def test_error_response_format_invalid_login(client: AsyncClient):
    r = await client.post("/api/v1/auth/login", json={"username": "nobody", "password": "wrong"})

    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHORIZED"
    assert r.headers["www-authenticate"] == "Bearer"


@pytest.mark.asyncio
async def test_error_response_format_validation_error(api: Api):
    r = await api.post("/tokens", "owner", json={"name": ""})

    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert "body.name" in body["error"]["message"]


@pytest.mark.asyncio
async def test_error_response_format_invalid_package_carries_a_reason(api: Api):
    r = await api.upload(b"not a zip at all", "owner")

    assert r.status_code == 422
    assert r.json()["error"] == {
        "code": "INVALID_PACKAGE",
        "message": r.json()["error"]["message"],
        "reason": "NOT_A_ZIP",
    }


@pytest.mark.asyncio
async def test_cors_headers_present(client: AsyncClient):
    r = await client.options(
        "/api/v1/health",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert "access-control-allow-origin" in r.headers
    assert r.headers["access-control-allow-origin"] == "http://localhost:5173"


@pytest.mark.asyncio
async def test_cors_headers_for_allowed_origin(client: AsyncClient):
    r = await client.get(
        "/api/v1/health",
        headers={"Origin": "http://localhost:5173"},
    )
    assert "access-control-allow-origin" in r.headers
    assert r.headers["access-control-allow-origin"] == "http://localhost:5173"


@pytest.mark.asyncio
async def test_cors_preflight(client: AsyncClient):
    r = await client.options(
        "/api/v1/skills",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Authorization, Content-Type",
        },
    )
    assert r.status_code in (200, 405)
    assert "access-control-allow-methods" in r.headers


@pytest.mark.asyncio
async def test_cors_preflight_for_the_upload_route(client: AsyncClient):
    r = await client.options(
        "/api/v1/packages",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Authorization, Content-Type",
        },
    )

    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "POST" in r.headers["access-control-allow-methods"]


@pytest.mark.asyncio
async def test_error_responses_keep_their_cors_headers(client: AsyncClient):
    r = await client.get("/api/v1/skills/nonexistent", headers={"Origin": "http://localhost:5173"})

    assert r.status_code == 404
    assert r.headers["access-control-allow-origin"] == "http://localhost:5173"


@pytest.mark.asyncio
async def test_the_body_limit_response_keeps_its_cors_headers(client: AsyncClient):
    r = await client.post(
        "/api/v1/tokens",
        content=b"{}",
        headers={
            "Origin": "http://localhost:5173",
            "Content-Type": "application/json",
            "Content-Length": str(10**9),
        },
    )

    assert r.status_code == 413
    assert r.headers["access-control-allow-origin"] == "http://localhost:5173"


@pytest.mark.asyncio
async def test_upload_logs_the_package(api: Api, caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.INFO, logger="app.services.package"):
        await upload(api, "log-skill")

    assert any("Package uploaded: log-skill 1.0.0" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_service_logs_on_auth_success(
    client: AsyncClient, db_session: AsyncSession, caplog: pytest.LogCaptureFixture
):
    await AuthService(db_session).create_user(
        UserCreate(username="logauth", email="logauth@test.com", password="pass123456")
    )
    with caplog.at_level(logging.INFO, logger="app.services.auth"):
        await client.post(
            "/api/v1/auth/login",
            json={"username": "logauth", "password": "pass123456"},
        )
    assert any("User authenticated: logauth" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_service_logs_on_auth_failure(client: AsyncClient, caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.WARNING, logger="app.services.auth"):
        await client.post(
            "/api/v1/auth/login",
            json={"username": "baduser", "password": "badpass"},
        )
    assert any("Failed login attempt" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_a_rejected_credential_is_logged_without_the_credential(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
):
    with caplog.at_level(logging.WARNING, logger="app.api.deps"):
        await client.get("/api/v1/skills", headers={"Authorization": "Bearer super-secret-value"})

    records = [r for r in caplog.records if r.name == "app.api.deps"]
    assert records and records[0].levelno == logging.WARNING
    assert "super-secret-value" not in json.dumps(records[0].__dict__, default=str)
    assert records[0].extra_data == {"method": "GET", "path": "/api/v1/skills"}


@pytest.mark.asyncio
async def test_request_logging(client: AsyncClient, caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.INFO, logger="app.request"):
        await client.get("/api/v1/health")
    assert any("/api/v1/health" in record.message for record in caplog.records)
    assert any("200" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_request_logging_sees_the_error_status(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
):
    with caplog.at_level(logging.INFO, logger="app.request"):
        await client.get("/api/v1/skills/missing")

    assert any("404" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_structured_log_format(api: Api, caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.INFO, logger="app.services.package"):
        await upload(api, "struct-skill")
    record = next(
        (r for r in caplog.records if "Package uploaded: struct-skill" in r.message), None
    )

    assert record is not None
    assert hasattr(record, "extra_data")
    assert {"skill_id", "version_id", "name", "version", "kind", "user_id"} <= set(
        record.extra_data
    )
    rendered = json.loads(JSONFormatter().format(record))
    assert rendered["logger"] == "app.services.package"
    assert rendered["level"] == "INFO"
    assert rendered["data"]["name"] == "struct-skill"


@pytest.mark.asyncio
async def test_version_logs_on_publish(api: Api, caplog: pytest.LogCaptureFixture):
    await upload(api, "pub-log")
    with caplog.at_level(logging.INFO, logger="app.services.version"):
        await api.post("/skills/pub-log/versions/1.0.0/publish", "owner")
    assert any("Version published" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_a_storage_failure_is_logged_as_an_error_and_never_shown(
    api: Api, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    async def broken(self, principal, ref):
        raise StorageError("blob /data/blobs/ab/abcdef.zip vanished")

    monkeypatch.setattr(SkillService, "get", broken)

    with caplog.at_level(logging.ERROR):
        response = await api.get("/skills/anything")

    assert_error(response, 500, "STORAGE_ERROR")
    assert "/data/blobs" not in response.text
    assert any(
        record.levelno == logging.ERROR and "vanished" in record.getMessage()
        for record in caplog.records
    )


PG_URL = os.environ.get("DATABASE_URL", "")


@pytest.mark.skipif(
    not PG_URL or "postgresql" not in PG_URL,
    reason="PostgreSQL not available (set DATABASE_URL to postgresql://... to enable)",
)
@pytest.mark.asyncio
async def test_pg_migration_creates_tables():
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.db.base import Base

    engine = create_async_engine(PG_URL, echo=False, future=True)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with engine.begin() as conn:
            result = await conn.run_sync(
                lambda sync_conn: sync_conn.execute(
                    __import__("sqlalchemy").text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public' ORDER BY table_name"
                    )
                ).fetchall()
            )
        table_names = [row[0] for row in result]
        assert "users" in table_names
        assert "skills" in table_names
        assert "skill_versions" in table_names
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()


@pytest.mark.skipif(
    not PG_URL or "postgresql" not in PG_URL,
    reason="PostgreSQL not available (set DATABASE_URL to postgresql://... to enable)",
)
@pytest.mark.asyncio
async def test_pg_migrations_run_alembic():
    from alembic import command
    from alembic.config import Config
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.db.base import Base

    engine = create_async_engine(PG_URL, echo=False, future=True)
    try:
        alembic_cfg = Config("alembic.ini")
        alembic_cfg.set_main_option("sqlalchemy.url", PG_URL)
        command.upgrade(alembic_cfg, "head")

        async with engine.begin() as conn:
            result = await conn.run_sync(
                lambda sync_conn: sync_conn.execute(
                    __import__("sqlalchemy").text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public' ORDER BY table_name"
                    )
                ).fetchall()
            )
        table_names = [row[0] for row in result]
        assert "users" in table_names
        assert "skills" in table_names
        assert "skill_versions" in table_names
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()
