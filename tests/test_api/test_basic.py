from __future__ import annotations

import pytest

from tests.test_api.support import Api, Catalog, assert_error


@pytest.mark.asyncio
async def test_health_is_public_and_reports_ok(api: Api):
    response = await api.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_an_empty_catalog_lists_nothing(api: Api):
    response = await api.get("/skills")

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "limit": 20, "offset": 0}


@pytest.mark.asyncio
async def test_the_seeded_catalog_is_reachable(api: Api, catalog: Catalog):
    response = await api.get("/skills", "owner")

    assert response.status_code == 200
    assert response.json()["total"] == 4


@pytest.mark.asyncio
async def test_an_unknown_route_answers_with_the_error_format(api: Api):
    response = await api.get("/nowhere")

    assert_error(response, 404, "NOT_FOUND")


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/docs", "/openapi.json"])
async def test_the_interactive_documentation_is_served(api: Api, path: str):
    response = await api.get(path)

    assert response.status_code == 200
