"""HTTP contract for reopening and resubmitting suggestion revisions."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from ontokit.api.routes.suggestions import get_service
from ontokit.core.auth import CurrentUser, get_current_user, get_current_user_optional
from ontokit.main import app
from ontokit.schemas.suggestion import SuggestionQueue
from ontokit.schemas.trust import TrustTier

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
        {
            get_service: service_dependency,
            get_current_user: user_dependency,
            get_current_user_optional: user_dependency,
        },
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


@pytest.mark.parametrize(
    "action,method,body,params",
    [
        ("approve", "approve", None, {}),
        ("reject", "reject", {"reason": "Duplicate concept"}, {}),
        ("request-changes", "request_changes", {"feedback": "Clarify the label"}, {}),
        ("dismiss", "dismiss", None, {"note": "Already covered"}),
    ],
)
async def test_review_decisions_return_empty_204(
    route_service: AsyncMock, action: str, method: str, body: dict | None, params: dict
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/projects/{PROJECT_ID}/suggestions/sessions/s_test/{action}",
            json=body,
            params=params,
        )

    assert response.status_code == 204
    assert response.content == b""
    operation = getattr(route_service, method)
    operation.assert_awaited_once()
    args = operation.await_args.args
    assert args[:2] == (PROJECT_ID, "s_test")
    if body is not None:
        assert args[2].model_dump() == body
        assert args[3] == CurrentUser(id="contributor")
    else:
        assert args[2] == CurrentUser(id="contributor")
        if action == "dismiss":
            assert args[3] == "Already covered"


@pytest.mark.parametrize(
    "action,body,params,location",
    [
        ("reject", {}, {}, ["body", "reason"]),
        ("reject", {"reason": ""}, {}, ["body", "reason"]),
        ("request-changes", {}, {}, ["body", "feedback"]),
        ("request-changes", {"feedback": ""}, {}, ["body", "feedback"]),
        ("dismiss", None, {"note": "x" * 1001}, ["query", "note"]),
    ],
)
async def test_invalid_review_payload_does_not_execute(
    route_service: AsyncMock,
    action: str,
    body: dict | None,
    params: dict,
    location: list[str],
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/projects/{PROJECT_ID}/suggestions/sessions/s_test/{action}",
            json=body,
            params=params,
        )

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == location
    assert route_service.mock_calls == []


@pytest.mark.parametrize("queue", [None, "triage", "review"])
async def test_pending_queue_accepts_declared_values(
    route_service: AsyncMock, queue: str | None
) -> None:
    route_service.list_pending.return_value = {"items": []}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            f"/api/v1/projects/{PROJECT_ID}/suggestions/pending",
            params={"queue": queue} if queue is not None else {},
        )

    assert response.status_code == 200
    assert response.json() == {"items": []}
    route_service.list_pending.assert_awaited_once_with(
        PROJECT_ID, CurrentUser(id="contributor"), SuggestionQueue(queue) if queue else None
    )


async def test_pending_queue_rejects_unknown_value(route_service: AsyncMock) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            f"/api/v1/projects/{PROJECT_ID}/suggestions/pending", params={"queue": "all"}
        )

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", "queue"]
    route_service.list_pending.assert_not_awaited()


async def test_capabilities_serializes_trust_and_verification(route_service: AsyncMock) -> None:
    route_service.get_capabilities.return_value = {
        "tier": TrustTier.UNTRUSTED,
        "can_suggest": True,
        "can_mint_entities": False,
        "promotion_threshold": 5,
        "accepted_count": 2,
        "auto_accept_enabled": True,
        "auto_accept_quiet_days": 7,
        "verification_required": True,
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/v1/projects/{PROJECT_ID}/suggestions/capabilities")

    assert response.status_code == 200
    assert response.json() == {
        "tier": "untrusted",
        "can_suggest": True,
        "can_mint_entities": False,
        "promotion_threshold": 5,
        "accepted_count": 2,
        "auto_accept_enabled": True,
        "auto_accept_quiet_days": 7,
        "verification_required": True,
    }
    route_service.get_capabilities.assert_awaited_once_with(
        PROJECT_ID, CurrentUser(id="contributor")
    )


async def test_session_list_serializes_review_metadata(route_service: AsyncMock) -> None:
    activity = datetime(2026, 10, 27, 12, 30, tzinfo=UTC)
    route_service.list_sessions.return_value = {
        "items": [
            {
                "session_id": "s_test",
                "branch": "suggest/contributor/s_test",
                "changes_count": 2,
                "last_activity": activity,
                "entities_modified": ["Example concept"],
                "status": "submitted",
                "pr_number": 12,
                "pr_url": None,
                "submitter_tier": TrustTier.TRUSTED,
                "auto_accept_after": activity,
                "reviewed_at": None,
                "is_llm_generated": True,
                "revision": 3,
            }
        ]
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/v1/projects/{PROJECT_ID}/suggestions/sessions")

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["session_id"] == "s_test"
    assert item["changes_count"] == 2
    assert item["entities_modified"] == ["Example concept"]
    assert item["submitter_tier"] == "trusted"
    assert item["last_activity"] == "2026-10-27T12:30:00Z"
    assert item["auto_accept_after"] == "2026-10-27T12:30:00Z"
    assert item["pr_number"] == 12
    assert item["pr_url"] is None
    assert item["reviewed_at"] is None
    assert item["is_llm_generated"] is True
    assert item["revision"] == 3


@pytest.mark.parametrize("action", ["accept", "dismiss"])
async def test_bulk_review_preserves_partial_success(route_service: AsyncMock, action: str) -> None:
    result = {
        "action": action,
        "succeeded": ["s_valid"],
        "failed": [{"session_id": "s_stale", "reason": "Session is already closed"}],
    }
    route_service.bulk_review.return_value = result
    body = {"action": action, "session_ids": ["s_stale", "s_valid"], "note": "Triage pass"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/projects/{PROJECT_ID}/suggestions/bulk-review", json=body
        )

    assert response.status_code == 200
    assert response.json() == result
    route_service.bulk_review.assert_awaited_once()
    args = route_service.bulk_review.await_args.args
    assert args[0] == PROJECT_ID
    assert args[1].model_dump(mode="json") == body
    assert args[2] == CurrentUser(id="contributor")


@pytest.mark.parametrize(
    "body,field",
    [
        ({"action": "reject", "session_ids": ["s_test"]}, "action"),
        ({"action": "dismiss", "session_ids": []}, "session_ids"),
        ({"action": "accept", "session_ids": [f"s_{i}" for i in range(101)]}, "session_ids"),
    ],
)
async def test_invalid_bulk_review_does_not_execute(
    route_service: AsyncMock, body: dict, field: str
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/projects/{PROJECT_ID}/suggestions/bulk-review", json=body
        )

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", field]
    route_service.bulk_review.assert_not_awaited()


@pytest.mark.parametrize("action", ["submit", "resubmit"])
async def test_submission_pool_failure_preserves_gates_and_service_refusal(
    route_service: AsyncMock, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    pool = AsyncMock(side_effect=ConnectionError("synthetic pool failure"))
    monkeypatch.setattr("ontokit.api.routes.suggestions.get_arq_pool", pool)
    operation = getattr(route_service, action)
    operation.side_effect = HTTPException(503, "Submission limiter unavailable")
    transport = ASGITransport(app=app, client=("192.0.2.42", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/projects/{PROJECT_ID}/suggestions/sessions/s_test/{action}",
            json={"summary": "Saved work"},
            headers={"X-Verification-Token": "synthetic-proof"},
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "Submission limiter unavailable"}
    pool.assert_awaited_once_with()
    operation.assert_awaited_once()
    assert operation.await_args.args[:2] == (PROJECT_ID, "s_test")
    assert operation.await_args.args[2].summary == "Saved work"
    assert operation.await_args.args[3] == CurrentUser(id="contributor")
    assert operation.await_args.kwargs == {
        "verification_token": "synthetic-proof",
        "client_ip": "192.0.2.42",
        "redis": None,
    }
