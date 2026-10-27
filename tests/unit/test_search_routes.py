"""HTTP validation and compatibility contracts for full-text search."""

from unittest.mock import AsyncMock, Mock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.api.routes.search import get_search_service
from ontokit.main import app
from ontokit.services.search import SearchService


@pytest.fixture
def route_search(monkeypatch: pytest.MonkeyPatch) -> tuple[AsyncMock, AsyncMock]:
    db = AsyncMock(spec=AsyncSession)
    db.execute.side_effect = [Mock(scalar_one=Mock(return_value=0)), []]
    service = SearchService(db=db)
    search = AsyncMock(wraps=service.search)
    monkeypatch.setattr(service, "search", search)

    async def dependency() -> SearchService:
        return service

    monkeypatch.setattr(app, "dependency_overrides", {get_search_service: dependency})
    return search, db


@pytest.mark.parametrize(
    ("params", "field", "error_type"),
    [
        ({}, "q", "missing"),
        ({"q": ""}, "q", "string_too_short"),
        ({"q": "Person", "limit": "0"}, "limit", "greater_than_equal"),
        ({"q": "Person", "limit": "101"}, "limit", "less_than_equal"),
        ({"q": "Person", "offset": "-1"}, "offset", "greater_than_equal"),
        ({"q": "Person", "limit": "many"}, "limit", "int_parsing"),
        ({"q": "Person", "offset": "next"}, "offset", "int_parsing"),
    ],
)
async def test_invalid_search_is_rejected_before_service_or_sql(
    route_search: tuple[AsyncMock, AsyncMock],
    params: dict[str, str],
    field: str,
    error_type: str,
) -> None:
    search, db = route_search
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/search", params=params)

    assert response.status_code == 422
    assert set(response.json()) == {"detail"}
    errors = response.json()["detail"]
    assert isinstance(errors, list)
    assert [(error["loc"], error["type"]) for error in errors] == [(["query", field], error_type)]
    search.assert_not_awaited()
    db.execute.assert_not_awaited()


@pytest.mark.parametrize("limit", [1, 100])
async def test_search_boundary_paging_reaches_service_unchanged(
    route_search: tuple[AsyncMock, AsyncMock], limit: int
) -> None:
    search, db = route_search
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/v1/search", params={"q": "Person", "limit": limit, "offset": 0}
        )

    assert response.status_code == 200
    data = response.json()
    assert set(data) == {"results", "total", "query", "took_ms"}
    assert data["results"] == []
    assert data["total"] == 0
    assert data["query"] == "Person"
    assert data["took_ms"] >= 0
    search.assert_awaited_once()
    assert search.call_args.args[0].model_dump() == {
        "query": "Person",
        "ontology_ids": None,
        "entity_types": None,
        "limit": limit,
        "offset": 0,
    }
    assert db.execute.await_count == 2


async def test_search_preserves_filters_and_default_paging(
    route_search: tuple[AsyncMock, AsyncMock],
) -> None:
    search, _ = route_search
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/v1/search",
            params={
                "q": "Person organization",
                "ontology_id": "first,second",
                "entity_types": "class,property",
            },
        )

    assert response.status_code == 200
    search.assert_awaited_once()
    assert search.call_args.args[0].model_dump() == {
        "query": "Person organization",
        "ontology_ids": ["first", "second"],
        "entity_types": ["class", "property"],
        "limit": 20,
        "offset": 0,
    }


@pytest.mark.parametrize("query", ["   ", ":::!!!"])
async def test_search_keeps_tokenless_queries_as_empty_success(
    route_search: tuple[AsyncMock, AsyncMock], query: str
) -> None:
    search, db = route_search
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/search", params={"q": query})

    assert response.status_code == 200
    data = response.json()
    assert data["results"] == []
    assert data["total"] == 0
    assert data["query"] == query
    assert data["took_ms"] >= 0
    search.assert_awaited_once()
    assert search.call_args.args[0].query == query
    db.execute.assert_not_awaited()
