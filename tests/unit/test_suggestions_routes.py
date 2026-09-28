"""HTTP contract for reopening and resubmitting suggestion revisions."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from ontokit.api.routes.suggestions import get_service
from ontokit.core.auth import CurrentUser, get_current_user
from ontokit.main import app

PROJECT_ID = UUID("12345678-1234-5678-1234-567812345678")


@pytest.fixture
def route_service(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    service = AsyncMock()

    async def service_dependency() -> AsyncMock:
        return service

    async def user_dependency() -> CurrentUser:
        return CurrentUser(id="contributor")

    monkeypatch.setattr(
        app,
        "dependency_overrides",
        {get_service: service_dependency, get_current_user: user_dependency},
    )
    return service


@pytest.mark.parametrize("status_code", [200, 400, 403, 409])
async def test_reopen_http_contract(route_service: AsyncMock, status_code: int) -> None:
    route_service.reopen.return_value = {
        "session_id": "s_test",
        "branch": "suggest/contributor/s_test",
        "created_at": datetime.now(UTC),
        "beacon_token": "fresh-token",
    }
    if status_code != 200:
        route_service.reopen.side_effect = HTTPException(status_code, "Refused")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/projects/{PROJECT_ID}/suggestions/sessions/s_test/reopen"
        )
    assert response.status_code == status_code
    route_service.reopen.assert_awaited_once_with(
        PROJECT_ID, "s_test", CurrentUser(id="contributor")
    )
    if status_code == 200:
        assert response.json()["beacon_token"] == "fresh-token"
        assert response.json()["branch"] == "suggest/contributor/s_test"


async def test_resubmit_passes_submission_gates(
    route_service: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = AsyncMock()
    monkeypatch.setattr(
        "ontokit.api.routes.suggestions.get_arq_pool", AsyncMock(return_value=redis)
    )
    route_service.resubmit.return_value = {"pr_number": 12, "status": "submitted"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/projects/{PROJECT_ID}/suggestions/sessions/s_test/resubmit",
            json={"summary": "Fixed"},
            headers={"X-Verification-Token": "proof"},
        )
    assert response.status_code == 200
    assert response.json()["pr_number"] == 12
    call = route_service.resubmit.call_args
    assert call.args[:2] == (PROJECT_ID, "s_test")
    assert call.args[2].summary == "Fixed"
    assert call.kwargs == {"verification_token": "proof", "client_ip": "127.0.0.1", "redis": redis}
