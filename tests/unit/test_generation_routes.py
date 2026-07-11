"""Endpoint tests for the generation routes (PR-5).

POST /projects/{id}/llm/generate-suggestions is the enforcement boundary for
LLM usage: role gate (403), rate limit (429), and budget (402) must all fire
BEFORE any LLM call. These tests pin that ordering — especially the 429 path,
which closes the PR-4 follow-up ("rate limiter shipped but not wired to
enforcement").

POST /projects/{id}/llm/validate-entity is pure server-side validation and
must work without any LLM configuration.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from fastapi.testclient import TestClient

from ontokit.schemas.generation import (
    GeneratedSuggestion,
    GenerateSuggestionsResponse,
    ValidationError,
)

PROJECT_ID = "11111111-1111-1111-1111-111111111111"
GENERATE_URL = f"/api/v1/projects/{PROJECT_ID}/llm/generate-suggestions"
VALIDATE_URL = f"/api/v1/projects/{PROJECT_ID}/llm/validate-entity"

GENERATE_BODY = {
    "class_iri": "http://example.org/ontology#Contract",
    "branch": "main",
    "suggestion_type": "children",
    "batch_size": 3,
}


def _scalar_one_or_none(value: object) -> Mock:
    result = Mock()
    result.scalar_one_or_none = Mock(return_value=value)
    return result


def _member(role: str) -> Mock:
    member = Mock()
    member.role = role
    return member


def _project(ontology_iri: str | None = "http://example.org/ontology#") -> Mock:
    project = Mock()
    project.id = PROJECT_ID
    project.ontology_iri = ontology_iri
    return project


def _llm_config(
    provider: str = "anthropic",
    api_key_encrypted: str | None = None,
    model: str | None = "claude-sonnet-4-5",
) -> Mock:
    config = Mock()
    config.provider = provider
    config.api_key_encrypted = api_key_encrypted
    config.model = model
    config.base_url = None
    config.monthly_budget_usd = 100.0
    config.daily_cap_usd = None
    return config


def _happy_path_execute(session: AsyncMock, role: str = "editor") -> None:
    """Wire session.execute for project → membership → llm-config lookups."""
    session.execute = AsyncMock(
        side_effect=[
            _scalar_one_or_none(_project()),  # _load_project
            _scalar_one_or_none(_member(role)),  # _require_project_member
            _scalar_one_or_none(_llm_config()),  # _get_llm_config
        ]
    )


def _generation_response() -> GenerateSuggestionsResponse:
    return GenerateSuggestionsResponse(
        suggestions=[
            GeneratedSuggestion(
                iri="http://example.org/ontology#abc123",
                suggestion_type="children",
                label="Employment Contract",
                definition="A contract governing an employment relationship.",
                confidence=0.9,
                provenance="llm-proposed",
                model="claude-sonnet-4-5",
                prompt_template="children",
            )
        ],
        input_tokens=120,
        output_tokens=60,
        context_tokens_estimate=None,
    )


# ── generate-suggestions: auth + membership ──────────────────────────────────


def test_generate_requires_auth(client: TestClient):
    resp = client.post(GENERATE_URL, json=GENERATE_BODY)
    assert resp.status_code in (401, 403)


def test_generate_404_unknown_project(authed_client: tuple[TestClient, AsyncMock]):
    client, session = authed_client
    session.execute = AsyncMock(side_effect=[_scalar_one_or_none(None)])

    resp = client.post(GENERATE_URL, json=GENERATE_BODY)
    assert resp.status_code == 404


def test_generate_403_for_non_member(authed_client: tuple[TestClient, AsyncMock]):
    client, session = authed_client
    session.execute = AsyncMock(
        side_effect=[
            _scalar_one_or_none(_project()),
            _scalar_one_or_none(None),  # not a member
        ]
    )

    resp = client.post(GENERATE_URL, json=GENERATE_BODY)
    assert resp.status_code == 403


def test_generate_403_for_viewer_role_gate(authed_client: tuple[TestClient, AsyncMock]):
    """ROLE-05: viewers are blocked by the LLM access gate even as members."""
    client, session = authed_client
    session.execute = AsyncMock(
        side_effect=[
            _scalar_one_or_none(_project()),
            _scalar_one_or_none(_member("viewer")),
        ]
    )

    resp = client.post(GENERATE_URL, json=GENERATE_BODY)
    assert resp.status_code == 403
    assert "role" in resp.json()["detail"].lower()


# ── generate-suggestions: config + enforcement ────────────────────────────────


def test_generate_400_when_no_llm_config(authed_client: tuple[TestClient, AsyncMock]):
    client, session = authed_client
    session.execute = AsyncMock(
        side_effect=[
            _scalar_one_or_none(_project()),
            _scalar_one_or_none(_member("editor")),
            _scalar_one_or_none(None),  # no LLM config
        ]
    )

    resp = client.post(GENERATE_URL, json=GENERATE_BODY)
    assert resp.status_code == 400
    assert "configuration" in resp.json()["detail"].lower()


def test_generate_422_on_invalid_batch_size(authed_client: tuple[TestClient, AsyncMock]):
    client, _session = authed_client
    resp = client.post(GENERATE_URL, json={**GENERATE_BODY, "batch_size": 0})
    assert resp.status_code == 422


def test_generate_429_when_rate_limit_exceeded(authed_client: tuple[TestClient, AsyncMock]):
    """Rate-limit enforcement fires with 429 BEFORE any LLM work (PR-4 follow-up)."""
    client, session = authed_client
    _happy_path_execute(session)

    with (
        patch("ontokit.api.routes.generation._get_redis", return_value=MagicMock()),
        patch(
            "ontokit.api.routes.generation.check_rate_limit",
            new=AsyncMock(return_value=False),
        ) as rate_mock,
        patch("ontokit.api.routes.generation.get_provider") as provider_mock,
        patch(
            "ontokit.api.routes.generation.check_budget",
            new=AsyncMock(return_value=(True, None)),
        ) as budget_mock,
    ):
        resp = client.post(GENERATE_URL, json=GENERATE_BODY)

    assert resp.status_code == 429
    assert "limit" in resp.json()["detail"].lower()
    rate_mock.assert_awaited_once()
    # Enforcement must precede any LLM call: no provider was ever constructed,
    # and the budget check (step 5) was never reached.
    provider_mock.assert_not_called()
    budget_mock.assert_not_awaited()


def test_generate_fails_open_when_redis_unavailable(
    authed_client: tuple[TestClient, AsyncMock],
    caplog,
):
    """No Redis → rate limiting is skipped (fail-open), pipeline continues,
    and the bypass is logged at WARNING so ops can alert on unmetered LLM
    traffic (PR-4 rate-limiter fail-open follow-up — the alertable path)."""
    client, session = authed_client
    _happy_path_execute(session)

    svc_instance = MagicMock()
    svc_instance.generate = AsyncMock(return_value=_generation_response())

    with (
        patch("ontokit.api.routes.generation._get_redis", return_value=None),
        patch(
            "ontokit.api.routes.generation.check_rate_limit",
            new=AsyncMock(return_value=False),
        ) as rate_mock,
        patch(
            "ontokit.api.routes.generation.check_budget",
            new=AsyncMock(return_value=(True, None)),
        ),
        patch("ontokit.api.routes.generation.get_provider", return_value=MagicMock()),
        patch(
            "ontokit.api.routes.generation.SuggestionGenerationService",
            return_value=svc_instance,
        ),
        patch(
            "ontokit.api.routes.generation.get_model_pricing",
            new=AsyncMock(return_value=(0.0, 0.0)),
        ),
        patch("ontokit.api.routes.generation.log_llm_call", new=AsyncMock()),
        caplog.at_level(logging.WARNING, logger="ontokit.api.routes.generation"),
    ):
        resp = client.post(GENERATE_URL, json=GENERATE_BODY)

    assert resp.status_code == 200
    rate_mock.assert_not_awaited()
    from ontokit.services.llm.rate_limiter import FAIL_OPEN_EVENT

    alerts = [
        rec
        for rec in caplog.records
        if getattr(rec, "event", None) == FAIL_OPEN_EVENT and rec.levelname == "WARNING"
    ]
    assert alerts, "fail-open Redis bypass must emit the actionable alert marker"
    assert alerts[0].operation == "route_rate_limit_bypass"


def test_generate_402_when_budget_exhausted(authed_client: tuple[TestClient, AsyncMock]):
    client, session = authed_client
    _happy_path_execute(session)

    with (
        patch("ontokit.api.routes.generation._get_redis", return_value=None),
        patch(
            "ontokit.api.routes.generation.check_budget",
            new=AsyncMock(return_value=(False, "budget_exhausted")),
        ),
        patch("ontokit.api.routes.generation.get_provider") as provider_mock,
    ):
        resp = client.post(GENERATE_URL, json=GENERATE_BODY)

    assert resp.status_code == 402
    assert "budget" in resp.json()["detail"].lower()
    provider_mock.assert_not_called()


def test_generate_402_when_daily_cap_reached(authed_client: tuple[TestClient, AsyncMock]):
    client, session = authed_client
    _happy_path_execute(session)

    with (
        patch("ontokit.api.routes.generation._get_redis", return_value=None),
        patch(
            "ontokit.api.routes.generation.check_budget",
            new=AsyncMock(return_value=(False, "daily_cap_reached")),
        ),
    ):
        resp = client.post(GENERATE_URL, json=GENERATE_BODY)

    assert resp.status_code == 402
    assert "daily" in resp.json()["detail"].lower()


# ── generate-suggestions: pipeline outcomes ───────────────────────────────────


def test_generate_success_shape(authed_client: tuple[TestClient, AsyncMock]):
    """Success returns suggestions with provenance, model, prompt_template, and token counts."""
    client, session = authed_client
    _happy_path_execute(session)

    svc_instance = MagicMock()
    svc_instance.generate = AsyncMock(return_value=_generation_response())

    with (
        patch("ontokit.api.routes.generation._get_redis", return_value=None),
        patch(
            "ontokit.api.routes.generation.check_budget",
            new=AsyncMock(return_value=(True, None)),
        ),
        patch("ontokit.api.routes.generation.get_provider", return_value=MagicMock()),
        patch(
            "ontokit.api.routes.generation.SuggestionGenerationService",
            return_value=svc_instance,
        ),
        patch(
            "ontokit.api.routes.generation.get_model_pricing",
            new=AsyncMock(return_value=(0.000001, 0.000002)),
        ),
        patch("ontokit.api.routes.generation.log_llm_call", new=AsyncMock()) as audit_mock,
    ):
        resp = client.post(GENERATE_URL, json=GENERATE_BODY)

    assert resp.status_code == 200
    body = resp.json()
    assert body["input_tokens"] == 120
    assert body["output_tokens"] == 60
    assert len(body["suggestions"]) == 1
    sug = body["suggestions"][0]
    assert sug["provenance"] == "llm-proposed"
    assert sug["suggestion_type"] == "children"
    assert 0.0 <= sug["confidence"] <= 1.0
    assert sug["model"] == "claude-sonnet-4-5"
    assert sug["prompt_template"] == "children"
    # The configured model id is threaded into the pipeline for provenance
    assert svc_instance.generate.await_args.kwargs["model_id"] == "claude-sonnet-4-5"
    audit_mock.assert_awaited_once()


def test_generate_404_when_class_not_in_index(authed_client: tuple[TestClient, AsyncMock]):
    """ValueError from context assembly (unknown class_iri) maps to 404."""
    client, session = authed_client
    _happy_path_execute(session)

    svc_instance = MagicMock()
    svc_instance.generate = AsyncMock(side_effect=ValueError("Class not found in index"))

    with (
        patch("ontokit.api.routes.generation._get_redis", return_value=None),
        patch(
            "ontokit.api.routes.generation.check_budget",
            new=AsyncMock(return_value=(True, None)),
        ),
        patch("ontokit.api.routes.generation.get_provider", return_value=MagicMock()),
        patch(
            "ontokit.api.routes.generation.SuggestionGenerationService",
            return_value=svc_instance,
        ),
    ):
        resp = client.post(GENERATE_URL, json=GENERATE_BODY)

    assert resp.status_code == 404


def test_generate_502_on_provider_auth_error(authed_client: tuple[TestClient, AsyncMock]):
    """Provider auth failures map to 502 with the API key redacted."""
    client, session = authed_client
    _happy_path_execute(session)

    svc_instance = MagicMock()
    svc_instance.generate = AsyncMock(side_effect=RuntimeError("401 unauthorized: bad api key"))

    with (
        patch("ontokit.api.routes.generation._get_redis", return_value=None),
        patch(
            "ontokit.api.routes.generation.check_budget",
            new=AsyncMock(return_value=(True, None)),
        ),
        patch("ontokit.api.routes.generation.get_provider", return_value=MagicMock()),
        patch(
            "ontokit.api.routes.generation.SuggestionGenerationService",
            return_value=svc_instance,
        ),
    ):
        resp = client.post(GENERATE_URL, json=GENERATE_BODY)

    assert resp.status_code == 502


# ── validate-entity ───────────────────────────────────────────────────────────


def test_validate_entity_requires_auth(client: TestClient):
    resp = client.post(
        VALIDATE_URL,
        json={"label": "X", "parent_iris": [], "labels": []},
    )
    assert resp.status_code in (401, 403)


def test_validate_entity_403_for_non_member(authed_client: tuple[TestClient, AsyncMock]):
    client, session = authed_client
    session.execute = AsyncMock(
        side_effect=[
            _scalar_one_or_none(_project()),
            _scalar_one_or_none(None),  # not a member
        ]
    )

    resp = client.post(
        VALIDATE_URL,
        json={"label": "X", "parent_iris": [], "labels": []},
    )
    assert resp.status_code == 403


def test_validate_entity_returns_structured_errors(
    authed_client: tuple[TestClient, AsyncMock],
):
    """validate-entity works without LLM config and returns VALID-05 errors."""
    client, session = authed_client
    session.execute = AsyncMock(
        side_effect=[
            _scalar_one_or_none(_project()),
            _scalar_one_or_none(_member("editor")),
        ]
    )

    validator = MagicMock()
    validator.validate_entity = AsyncMock(
        return_value=[
            ValidationError(field="parent_iris", code="VALID-01", message="Parent required")
        ]
    )

    with patch("ontokit.api.routes.generation.ValidationService", return_value=validator):
        resp = client.post(
            VALIDATE_URL,
            json={
                "label": "New Concept",
                "parent_iris": [],
                "labels": [{"lang": "en", "value": "New Concept"}],
                "namespace": "http://example.org/ontology#",
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is False
    assert body["errors"][0]["code"] == "VALID-01"


def test_validate_entity_valid_when_no_errors(authed_client: tuple[TestClient, AsyncMock]):
    client, session = authed_client
    session.execute = AsyncMock(
        side_effect=[
            _scalar_one_or_none(_project()),
            _scalar_one_or_none(_member("suggester")),
        ]
    )

    validator = MagicMock()
    validator.validate_entity = AsyncMock(return_value=[])

    with patch("ontokit.api.routes.generation.ValidationService", return_value=validator):
        resp = client.post(
            VALIDATE_URL,
            json={
                "label": "New Concept",
                "parent_iris": ["http://example.org/ontology#Parent"],
                "labels": [{"lang": "en", "value": "New Concept"}],
                "namespace": "http://example.org/ontology#",
            },
        )

    assert resp.status_code == 200
    assert resp.json() == {"valid": True, "errors": []}
