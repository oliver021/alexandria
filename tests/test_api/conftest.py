from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.storage import LocalBlobStore
from tests.helpers.factories import Actors
from tests.test_api.support import (
    JWT_SECRET,
    PUBLIC_URL,
    Api,
    BaseTemplate,
    Catalog,
    provide_base,
    provide_catalog,
)


@pytest.fixture
def api_settings(monkeypatch: pytest.MonkeyPatch, fast_password_hashing: None) -> None:
    monkeypatch.setattr(settings.auth, "jwt_secret", JWT_SECRET)
    monkeypatch.setattr(settings.registry, "public_read", True)
    monkeypatch.setattr(settings.registry, "auto_publish", False)
    monkeypatch.setattr(settings.server, "public_url", PUBLIC_URL)
    monkeypatch.setattr(settings.tokens, "max_per_user", 20)


@pytest_asyncio.fixture
async def world(db_session: AsyncSession, api_settings: None) -> BaseTemplate:
    return await provide_base(db_session)


@pytest.fixture
def people(world: BaseTemplate) -> Actors:
    return world.actors


@pytest.fixture
def tokens(world: BaseTemplate) -> dict[str, str | None]:
    return dict(world.tokens)


@pytest_asyncio.fixture
async def api(client: AsyncClient, tokens: dict[str, str | None]) -> Api:
    return Api(client, tokens)


@pytest_asyncio.fixture
async def lenient_api(client: AsyncClient, tokens: dict[str, str | None]) -> AsyncIterator[Api]:
    from app.main import app

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as lenient_client:
        yield Api(lenient_client, tokens)


@pytest_asyncio.fixture
async def catalog(
    db_session: AsyncSession, blob_store: LocalBlobStore, world: BaseTemplate
) -> Catalog:
    return await provide_catalog(db_session, blob_store, world.actors)


@pytest_asyncio.fixture
async def mini_catalog(
    db_session: AsyncSession, blob_store: LocalBlobStore, world: BaseTemplate
) -> Catalog:
    return await provide_catalog(db_session, blob_store, world.actors, mini=True)
