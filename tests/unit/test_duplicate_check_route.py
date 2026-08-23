"""Endpoint tests for POST /projects/{id}/duplicate-check (PR-6)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from ontokit.api.routes.duplicate_check import mark_distinct, revoke_distinct_decision
from ontokit.core.auth import ANONYMOUS_USER
from ontokit.schemas.duplicate_check import (
    DistinctDecisionMarkRequest,
    DistinctDecisionResponse,
    DuplicateCheckResponse,
    ScoreBreakdown,
)

PROJECT_ID = "11111111-1111-1111-1111-111111111111"
URL = f"/api/v1/projects/{PROJECT_ID}/duplicate-check"
DECISIONS_URL = f"{URL}/distinct-decisions"

BODY = {
    "label": "Employment Contract",
    "entity_type": "class",
    "parent_iri": "http://example.org/ontology#Contract",
}


def _pass_response() -> DuplicateCheckResponse:
    return DuplicateCheckResponse(
        verdict="pass",
        composite_score=0.0,
        score_breakdown=ScoreBreakdown(exact=0.0, semantic=0.0, structural=0.0),
        candidates=[],
    )


def _patch_access(
    allowed: bool = True, status_code: int = 403, user_role: str | None = "editor"
) -> MagicMock:
    service = MagicMock()
    if allowed:
        service.get = AsyncMock(return_value=MagicMock(user_role=user_role))
    else:
        service.get = AsyncMock(side_effect=HTTPException(status_code=status_code, detail="denied"))
    return service


# ── Access control (mirrors /search/semantic) ────────────────────────────────


def test_private_project_denied_before_check_runs(
    authed_client: tuple[TestClient, AsyncMock],
) -> None:
    """403 from the project access rule must fire before any scoring work."""
    with (
        patch(
            "ontokit.api.routes.duplicate_check.get_project_service",
            return_value=_patch_access(allowed=False, status_code=403),
        ),
        patch("ontokit.api.routes.duplicate_check.DuplicateCheckService") as service_cls,
    ):
        resp = authed_client[0].post(URL, json=BODY)

    assert resp.status_code == 403
    service_cls.assert_not_called()


def test_unknown_project_404(authed_client: tuple[TestClient, AsyncMock]) -> None:
    with patch(
        "ontokit.api.routes.duplicate_check.get_project_service",
        return_value=_patch_access(allowed=False, status_code=404),
    ):
        resp = authed_client[0].post(URL, json=BODY)

    assert resp.status_code == 404


def test_public_project_rejects_anonymous_before_check_runs(client: TestClient) -> None:
    """Anonymous callers cannot spend a public project's stored embedding key."""
    with patch("ontokit.api.routes.duplicate_check.DuplicateCheckService") as service_cls:
        resp = client.post(URL, json=BODY)

    assert resp.status_code == 401
    service_cls.assert_not_called()


# ── Wiring ────────────────────────────────────────────────────────────────────


def test_request_fields_forwarded_to_service(
    authed_client: tuple[TestClient, AsyncMock],
) -> None:
    """label / entity_type / parent_iri from the request reach the service."""
    check = AsyncMock(return_value=_pass_response())
    with (
        patch(
            "ontokit.api.routes.duplicate_check.get_project_service",
            return_value=_patch_access(allowed=True),
        ),
        patch("ontokit.api.routes.duplicate_check.DuplicateCheckService") as service_cls,
    ):
        service_cls.return_value.check = check
        resp = authed_client[0].post(URL, json=BODY)

    assert resp.status_code == 200
    check.assert_awaited_once()
    kwargs = check.await_args.kwargs
    assert kwargs["label"] == BODY["label"]
    assert kwargs["entity_type"] == BODY["entity_type"]
    assert kwargs["parent_iri"] == BODY["parent_iri"]
    assert kwargs["limit"] == 10


def test_branch_field_removed_and_never_forwarded(
    authed_client: tuple[TestClient, AsyncMock],
) -> None:
    """Pins the contract: `branch` is NOT a request field. Duplicate detection
    always searches ALL branches (DEDUP-08), so a per-request branch scope would
    be silently ignored — the field was removed. A stray `branch` in the body is
    dropped (extra fields ignored) and never reaches the service."""
    from ontokit.schemas.duplicate_check import DuplicateCheckRequest

    # The request model no longer declares `branch`.
    assert "branch" not in DuplicateCheckRequest.model_fields

    check = AsyncMock(return_value=_pass_response())
    with (
        patch(
            "ontokit.api.routes.duplicate_check.get_project_service",
            return_value=_patch_access(allowed=True),
        ),
        patch("ontokit.api.routes.duplicate_check.DuplicateCheckService") as service_cls,
    ):
        service_cls.return_value.check = check
        # A stray branch key is ignored by the schema, request still succeeds.
        resp = authed_client[0].post(URL, json={**BODY, "branch": "feature-x"})

    assert resp.status_code == 200
    assert "branch" not in check.await_args.kwargs


def test_422_on_missing_label(authed_client: tuple[TestClient, AsyncMock]) -> None:
    with patch(
        "ontokit.api.routes.duplicate_check.get_project_service",
        return_value=_patch_access(allowed=True),
    ):
        resp = authed_client[0].post(URL, json={"entity_type": "class"})

    assert resp.status_code == 422


def _decision_response() -> DistinctDecisionResponse:
    from datetime import UTC, datetime
    from uuid import UUID

    return DistinctDecisionResponse(
        id=UUID("22222222-2222-2222-2222-222222222222"),
        project_id=UUID(PROJECT_ID),
        iri_a="http://example.org/ontology#EmploymentContract",
        iri_b="http://example.org/ontology#WorkAgreement",
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


def test_editor_can_mark_distinct_and_inputs_are_forwarded(
    authed_client: tuple[TestClient, AsyncMock],
) -> None:
    mark = AsyncMock(return_value=_decision_response())
    body = {
        "proposed_iri": "http://example.org/ontology#EmploymentContract",
        "label": "Employment Contract",
        "candidate_iri": "http://example.org/ontology#WorkAgreement",
        "entity_type": "class",
        "parent_iri": "http://example.org/ontology#Contract",
        "reason": "They have different legal effects.",
    }
    with (
        patch(
            "ontokit.api.routes.duplicate_check.get_project_service",
            return_value=_patch_access(user_role="editor"),
        ),
        patch("ontokit.api.routes.duplicate_check.DuplicateCheckService") as service_cls,
    ):
        service_cls.return_value.mark_distinct = mark
        response = authed_client[0].post(DECISIONS_URL, json=body)

    assert response.status_code == 201
    assert response.json()["reason"] == body["reason"]
    assert mark.await_args.kwargs["actor_id"] == "test-user-id"
    assert mark.await_args.kwargs["request"].candidate_iri == body["candidate_iri"]


def test_suggester_cannot_mark_distinct(
    authed_client: tuple[TestClient, AsyncMock],
) -> None:
    with (
        patch(
            "ontokit.api.routes.duplicate_check.get_project_service",
            return_value=_patch_access(user_role="suggester"),
        ),
        patch("ontokit.api.routes.duplicate_check.DuplicateCheckService") as service_cls,
    ):
        response = authed_client[0].post(
            DECISIONS_URL,
            json={
                "proposed_iri": "http://example.org/A",
                "label": "A",
                "candidate_iri": "http://example.org/B",
                "reason": "Different concepts",
            },
        )

    assert response.status_code == 403
    service_cls.assert_not_called()


@pytest.mark.asyncio
async def test_anonymous_user_cannot_mark_distinct_before_project_or_service_access(
    mock_db_session: AsyncMock,
) -> None:
    project_service = _patch_access(user_role="owner")
    with (
        patch(
            "ontokit.api.routes.duplicate_check.get_project_service",
            return_value=project_service,
        ) as get_project_service,
        patch("ontokit.api.routes.duplicate_check.DuplicateCheckService") as service_cls,
        pytest.raises(HTTPException) as exc_info,
    ):
        await mark_distinct(
            project_id=UUID(PROJECT_ID),
            request=DistinctDecisionMarkRequest(
                proposed_iri="http://example.org/A",
                label="A",
                candidate_iri="http://example.org/B",
                reason="Different concepts",
            ),
            db=mock_db_session,
            user=ANONYMOUS_USER,
        )

    assert exc_info.value.status_code == 403
    get_project_service.assert_not_called()
    project_service.get.assert_not_awaited()
    service_cls.assert_not_called()


def test_mark_distinct_requires_non_empty_reason(
    authed_client: tuple[TestClient, AsyncMock],
) -> None:
    with patch(
        "ontokit.api.routes.duplicate_check.get_project_service",
        return_value=_patch_access(user_role="editor"),
    ):
        response = authed_client[0].post(
            DECISIONS_URL,
            json={
                "proposed_iri": "http://example.org/A",
                "label": "A",
                "candidate_iri": "http://example.org/B",
                "reason": "   ",
            },
        )

    assert response.status_code == 422


def test_editor_cannot_revoke_but_admin_can(
    authed_client: tuple[TestClient, AsyncMock],
) -> None:
    decision = _decision_response()
    revoke_url = f"{DECISIONS_URL}/{decision.id}"

    with patch(
        "ontokit.api.routes.duplicate_check.get_project_service",
        return_value=_patch_access(user_role="editor"),
    ):
        denied = authed_client[0].delete(revoke_url)
    assert denied.status_code == 403

    revoked = decision.model_copy(
        update={"revoked_at": decision.marked_at, "revoked_by": "test-user-id"}
    )
    with (
        patch(
            "ontokit.api.routes.duplicate_check.get_project_service",
            return_value=_patch_access(user_role="admin"),
        ),
        patch("ontokit.api.routes.duplicate_check.DuplicateCheckService") as service_cls,
    ):
        service_cls.return_value.revoke_distinct_decision = AsyncMock(return_value=revoked)
        allowed = authed_client[0].delete(revoke_url)

    assert allowed.status_code == 200
    assert allowed.json()["revoked_by"] == "test-user-id"


@pytest.mark.asyncio
async def test_anonymous_user_cannot_revoke_before_project_or_service_access(
    mock_db_session: AsyncMock,
) -> None:
    decision = _decision_response()
    project_service = _patch_access(user_role="owner")
    with (
        patch(
            "ontokit.api.routes.duplicate_check.get_project_service",
            return_value=project_service,
        ) as get_project_service,
        patch("ontokit.api.routes.duplicate_check.DuplicateCheckService") as service_cls,
        pytest.raises(HTTPException) as exc_info,
    ):
        await revoke_distinct_decision(
            project_id=UUID(PROJECT_ID),
            decision_id=decision.id,
            db=mock_db_session,
            user=ANONYMOUS_USER,
        )

    assert exc_info.value.status_code == 403
    get_project_service.assert_not_called()
    project_service.get.assert_not_awaited()
    service_cls.assert_not_called()
