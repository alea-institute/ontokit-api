"""Endpoint tests for POST /projects/{id}/duplicate-check (PR-6)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from ontokit.schemas.duplicate_check import (
    DuplicateCheckResponse,
    ScoreBreakdown,
)

PROJECT_ID = "11111111-1111-1111-1111-111111111111"
URL = f"/api/v1/projects/{PROJECT_ID}/duplicate-check"

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
        service.get = AsyncMock(
            side_effect=HTTPException(status_code=status_code, detail="denied")
        )
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
        patch(
            "ontokit.api.routes.duplicate_check.DuplicateCheckService"
        ) as service_cls,
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
        patch(
            "ontokit.api.routes.duplicate_check.DuplicateCheckService"
        ) as service_cls,
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
        patch(
            "ontokit.api.routes.duplicate_check.DuplicateCheckService"
        ) as service_cls,
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
