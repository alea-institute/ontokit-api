"""Tests for health check endpoint."""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from ontokit.main import app
from ontokit.schemas.health import DependencyChecks, HealthCheck
from ontokit.services.health import get_health_service


def test_health_check(client: TestClient) -> None:
    """Test that health check returns healthy status."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_root_endpoint(client: TestClient) -> None:
    """Test that root endpoint returns API info."""
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "OntoKit API"
    assert "version" in data
    assert "docs" in data


@pytest.mark.parametrize("database", ["ok", "unavailable", "timeout"])
@pytest.mark.parametrize("redis", ["ok", "unavailable", "timeout"])
async def test_readiness_status_and_independent_checks(
    monkeypatch: pytest.MonkeyPatch, database: str, redis: str
) -> None:
    checks = DependencyChecks(
        database=HealthCheck(status=database, message="Database check."),
        redis=HealthCheck(status=redis, message="Redis check."),
    )
    service = AsyncMock()
    service.check.return_value = checks

    async def dependency() -> AsyncMock:
        return service

    monkeypatch.setattr(app, "dependency_overrides", {get_health_service: dependency})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health/ready", headers={"X-Request-ID": "readiness-check"})
    ready = database == redis == "ok"
    assert response.status_code == (200 if ready else 503)
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"] == "readiness-check"
    assert response.json() == {
        "scope": "dependencies",
        "status": "ready" if ready else "not_ready",
        "request_id": "readiness-check",
        "checks": {
            "database": {"status": database, "message": "Database check."},
            "redis": {"status": redis, "message": "Redis check."},
        },
    }
    service.check.assert_awaited_once_with()


async def test_liveness_never_acquires_health_service(monkeypatch: pytest.MonkeyPatch) -> None:
    service_dependency = AsyncMock(side_effect=AssertionError("liveness must not probe"))
    monkeypatch.setattr(app, "dependency_overrides", {get_health_service: service_dependency})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}
    service_dependency.assert_not_called()


async def test_readiness_generates_matching_request_id(monkeypatch: pytest.MonkeyPatch) -> None:
    service = AsyncMock()
    service.check.return_value = DependencyChecks(
        database=HealthCheck(status="ok", message="Database available."),
        redis=HealthCheck(status="ok", message="Redis available."),
    )

    async def dependency() -> AsyncMock:
        return service

    monkeypatch.setattr(app, "dependency_overrides", {get_health_service: dependency})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["request_id"] == response.headers["x-request-id"]
    assert response.json()["request_id"]


def test_readiness_openapi_describes_dependency_scope() -> None:
    from fastapi.openapi.utils import get_openapi

    schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
    responses = schema["paths"]["/health/ready"]["get"]["responses"]
    for status in ["200", "503"]:
        assert responses[status]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ReadinessResponse"
        }
    assert (
        schema["components"]["schemas"]["ReadinessResponse"]["properties"]["scope"]["const"]
        == "dependencies"
    )
