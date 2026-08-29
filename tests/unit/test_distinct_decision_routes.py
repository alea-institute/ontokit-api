"""Authorization and wiring tests for distinct-decision endpoints."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

from fastapi.testclient import TestClient

from ontokit.schemas.duplicate_check import DistinctDecisionResponse

PROJECT_ID = "11111111-1111-1111-1111-111111111111"
URL = f"/api/v1/projects/{PROJECT_ID}/duplicate-check/distinct-decisions"


def _project_service(role: str | None) -> MagicMock:
    service = MagicMock()
    service.get = AsyncMock(return_value=MagicMock(user_role=role))
    return service


def _decision() -> DistinctDecisionResponse:
    return DistinctDecisionResponse(
        id=UUID("22222222-2222-2222-2222-222222222222"),
        project_id=UUID(PROJECT_ID),
        iri_a="https://example.test/EmploymentContract",
        iri_b="https://example.test/WorkAgreement",
        fingerprint_a="a" * 64,
        fingerprint_b="b" * 64,
        reason="They have different legal effects.",
        marked_by="test-user-id",
        marked_at=datetime.now(UTC),
        suggestion_session_id=None,
        revoked_at=None,
        revoked_by=None,
        superseded_by_id=None,
    )


def _body() -> dict[str, str]:
    return {
        "proposed_iri": "https://example.test/EmploymentContract",
        "label": "Employment Contract",
        "candidate_iri": "https://example.test/WorkAgreement",
        "candidate_branch": "main",
        "entity_type": "class",
        "reason": "They have different legal effects.",
    }


def test_editor_can_mark_current_candidate_distinct(
    authed_client: tuple[TestClient, AsyncMock],
) -> None:
    mark = AsyncMock(return_value=_decision())
    with (
        patch(
            "ontokit.api.routes.duplicate_check.get_project_service",
            return_value=_project_service("editor"),
        ),
        patch("ontokit.api.routes.duplicate_check.DuplicateCheckService") as service_cls,
    ):
        service_cls.return_value.mark_distinct = mark
        response = authed_client[0].post(URL, json=_body())

    assert response.status_code == 201
    assert mark.await_args.kwargs["actor_id"] == "test-user-id"
    assert mark.await_args.kwargs["billing_user_id"] == "test-user-id"
    assert mark.await_args.kwargs["request"].candidate_branch == "main"


def test_suggester_cannot_mark_distinct(
    authed_client: tuple[TestClient, AsyncMock],
) -> None:
    with (
        patch(
            "ontokit.api.routes.duplicate_check.get_project_service",
            return_value=_project_service("suggester"),
        ),
        patch("ontokit.api.routes.duplicate_check.DuplicateCheckService") as service_cls,
    ):
        response = authed_client[0].post(URL, json=_body())

    assert response.status_code == 403
    service_cls.assert_not_called()


def test_authenticated_member_can_list_active_decisions(
    authed_client: tuple[TestClient, AsyncMock],
) -> None:
    listed = AsyncMock(return_value=[_decision()])
    with (
        patch(
            "ontokit.api.routes.duplicate_check.get_project_service",
            return_value=_project_service("viewer"),
        ),
        patch("ontokit.api.routes.duplicate_check.DuplicateCheckService") as service_cls,
    ):
        service_cls.return_value.list_distinct_decisions = listed
        response = authed_client[0].get(URL)

    assert response.status_code == 200
    assert response.json()[0]["reason"] == "They have different legal effects."


def test_editor_cannot_revoke_but_admin_can(
    authed_client: tuple[TestClient, AsyncMock],
) -> None:
    decision = _decision()
    revoke_url = f"{URL}/{decision.id}"
    with patch(
        "ontokit.api.routes.duplicate_check.get_project_service",
        return_value=_project_service("editor"),
    ):
        denied = authed_client[0].delete(revoke_url)
    assert denied.status_code == 403

    revoked = decision.model_copy(
        update={"revoked_at": decision.marked_at, "revoked_by": "test-user-id"}
    )
    with (
        patch(
            "ontokit.api.routes.duplicate_check.get_project_service",
            return_value=_project_service("admin"),
        ),
        patch("ontokit.api.routes.duplicate_check.DuplicateCheckService") as service_cls,
    ):
        service_cls.return_value.revoke_distinct_decision = AsyncMock(return_value=revoked)
        allowed = authed_client[0].delete(revoke_url)

    assert allowed.status_code == 200
    assert allowed.json()["revoked_by"] == "test-user-id"
