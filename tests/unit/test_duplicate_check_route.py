"""Endpoint tests for POST /projects/{id}/duplicate-check (PR-6).

The route can invoke a paid embedding provider, so it requires an authenticated
LLM-capable project member before scoring begins.
"""

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


def _patch_access(allowed: bool = True, status_code: int = 403) -> MagicMock:
    service = MagicMock()
    if allowed:
        service.get = AsyncMock(return_value=MagicMock())
    else:
        service.get = AsyncMock(side_effect=HTTPException(status_code=status_code, detail="denied"))
    return service


# ── Access control (mirrors /search/semantic) ────────────────────────────────


def test_private_project_denied_before_check_runs(
    authed_client: tuple[TestClient, AsyncMock],
) -> None:
    """403 from the project access rule must fire before any scoring work."""
    client, _session = authed_client
    with (
        patch(
            "ontokit.api.routes.duplicate_check.get_project_service",
            return_value=_patch_access(allowed=False, status_code=403),
        ),
        patch("ontokit.api.routes.duplicate_check.DuplicateCheckService") as service_cls,
    ):
        resp = client.post(URL, json=BODY)

    assert resp.status_code == 403
    service_cls.assert_not_called()


def test_unknown_project_404(authed_client: tuple[TestClient, AsyncMock]) -> None:
    client, _session = authed_client
    with patch(
        "ontokit.api.routes.duplicate_check.get_project_service",
        return_value=_patch_access(allowed=False, status_code=404),
    ):
        resp = client.post(URL, json=BODY)

    assert resp.status_code == 404


def test_public_project_still_requires_authentication(client: TestClient) -> None:
    """Public visibility does not authorize spending a configured provider key."""
    with patch("ontokit.api.routes.duplicate_check.DuplicateCheckService") as service_cls:
        resp = client.post(URL, json=BODY)

    assert resp.status_code in (401, 403)
    service_cls.assert_not_called()


# ── Wiring ────────────────────────────────────────────────────────────────────


def test_request_fields_forwarded_to_service(
    authed_client: tuple[TestClient, AsyncMock],
) -> None:
    """label / entity_type / parent_iri from the request reach the service."""
    client, _session = authed_client
    check = AsyncMock(return_value=_pass_response())
    with (
        patch(
            "ontokit.api.routes.duplicate_check.get_project_service",
            return_value=_patch_access(allowed=True),
        ),
        patch(
            "ontokit.api.routes.duplicate_check.require_embedding_query_access",
            new=AsyncMock(return_value="editor"),
        ),
        patch("ontokit.api.routes.duplicate_check.DuplicateCheckService") as service_cls,
    ):
        service_cls.return_value.check = check
        resp = client.post(URL, json=BODY)

    assert resp.status_code == 200
    check.assert_awaited_once()
    kwargs = check.await_args.kwargs
    assert kwargs["label"] == BODY["label"]
    assert kwargs["entity_type"] == BODY["entity_type"]
    assert kwargs["parent_iri"] == BODY["parent_iri"]
    assert kwargs["limit"] == 10
    service_cls.assert_called_once_with(
        _session,
        billing_user_id="test-user-id",
    )


def test_branch_field_removed_and_never_forwarded(
    authed_client: tuple[TestClient, AsyncMock],
) -> None:
    """Pins the contract: `branch` is NOT a request field. Duplicate detection
    always searches ALL branches (DEDUP-08), so a per-request branch scope would
    be silently ignored — the field was removed. A stray `branch` in the body is
    dropped (extra fields ignored) and never reaches the service."""
    from ontokit.schemas.duplicate_check import DuplicateCheckRequest

    client, _session = authed_client

    # The request model no longer declares `branch`.
    assert "branch" not in DuplicateCheckRequest.model_fields

    check = AsyncMock(return_value=_pass_response())
    with (
        patch(
            "ontokit.api.routes.duplicate_check.get_project_service",
            return_value=_patch_access(allowed=True),
        ),
        patch(
            "ontokit.api.routes.duplicate_check.require_embedding_query_access",
            new=AsyncMock(return_value="editor"),
        ),
        patch("ontokit.api.routes.duplicate_check.DuplicateCheckService") as service_cls,
    ):
        service_cls.return_value.check = check
        # A stray branch key is ignored by the schema, request still succeeds.
        resp = client.post(URL, json={**BODY, "branch": "feature-x"})

    assert resp.status_code == 200
    assert "branch" not in check.await_args.kwargs


def test_422_on_missing_label(authed_client: tuple[TestClient, AsyncMock]) -> None:
    client, _session = authed_client
    with patch(
        "ontokit.api.routes.duplicate_check.get_project_service",
        return_value=_patch_access(allowed=True),
    ):
        resp = client.post(URL, json={"entity_type": "class"})

    assert resp.status_code == 422


def test_openapi_registers_documented_duplicate_check_path() -> None:
    """The schema documentation and registered route stay on one public contract."""
    from ontokit.main import app
    from ontokit.schemas.duplicate_check import DuplicateCheckRequest

    assert "POST /projects/{id}/duplicate-check" in (DuplicateCheckRequest.__doc__ or "")
    assert "/api/v1/projects/{project_id}/duplicate-check" in app.openapi()["paths"]
    assert "/api/v1/projects/{project_id}/duplicates/check" not in app.openapi()["paths"]
