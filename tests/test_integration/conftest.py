from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config.settings import settings
from app.db import session as database
from app.mcp import context
from app.storage import LocalBlobStore
from tests.test_integration.support import (
    JWT_SECRET,
    PUBLIC_URL,
    Mcp,
    Rest,
    World,
    provide_world,
)


@pytest.fixture(autouse=True)
def integration_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.auth, "jwt_secret", JWT_SECRET)
    monkeypatch.setattr(settings.registry, "public_read", True)
    monkeypatch.setattr(settings.registry, "auto_publish", False)
    monkeypatch.setattr(settings.server, "public_url", PUBLIC_URL)
    monkeypatch.setattr(settings.tokens, "max_per_user", 20)


@pytest_asyncio.fixture
async def world(
    session_factory: async_sessionmaker[AsyncSession], blob_store: LocalBlobStore
) -> World:
    return await provide_world(session_factory, blob_store)


@pytest.fixture
def rest(client: Any, world: World) -> Rest:
    return Rest(client, world.credentials)


@pytest.fixture
def mcp(client: Any, world: World, mcp_server: None) -> Mcp:
    return Mcp(client, world.credentials)


@pytest.fixture(autouse=True)
def mcp_runtime_is_released_after_every_test() -> Iterator[None]:
    yield
    assert context.get_session_factory() is database.async_session_factory
