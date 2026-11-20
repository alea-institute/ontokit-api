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

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, Mock, patch
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from ontokit.api.routes import generation
from ontokit.core.database import engine
from ontokit.main import app
from ontokit.schemas.generation import (
    GeneratedSuggestion,
    GenerateSuggestionsResponse,
    ValidationError,
)
from ontokit.services.llm.metering import LLMBudgetExceeded, MeteredLLMProvider

PROJECT_ID = "11111111-1111-1111-1111-111111111111"
GENERATE_URL = f"/api/v1/projects/{PROJECT_ID}/llm/generate-suggestions"
VALIDATE_URL = f"/api/v1/projects/{PROJECT_ID}/llm/validate-entity"

GENERATE_BODY = {
    "class_iri": "http://example.org/ontology#Contract",
    "branch": "main",
    "suggestion_type": "children",
    "batch_size": 3,
}


@pytest.fixture(autouse=True)
def mock_model_pricing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ontokit.api.routes.generation.get_model_pricing",
        AsyncMock(return_value=(0.000001, 0.000002)),
    )


@pytest.fixture(autouse=True)
def ledger_session(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    session = AsyncMock()
    session.add_all = Mock()
    session.__aenter__.return_value = session
    factory = Mock(return_value=session)
    monkeypatch.setattr(generation, "async_session_maker", factory)
    return session


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


def _happy_path_execute(
    session: AsyncMock, role: str = "editor", *, marker_rowcount: int = 1
) -> None:
    """Wire project → membership → config → provenance-marker results."""
    marker_result = MagicMock()
    marker_result.rowcount = marker_rowcount
    session.execute = AsyncMock(
        side_effect=[
            _scalar_one_or_none(_project()),  # _load_project
            _scalar_one_or_none(_member(role)),  # _require_project_member
            _scalar_one_or_none(_llm_config()),  # _get_llm_config
            marker_result,
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
    ):
        resp = client.post(GENERATE_URL, json=GENERATE_BODY)

    assert resp.status_code == 200
    body = resp.json()
    assert UUID(body["batch_id"]).version == 4
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
    assert isinstance(svc_instance.generate.await_args.kwargs["provider"], MeteredLLMProvider)


def test_success_marks_matching_active_suggestion_session_as_llm_generated(
    authed_client: tuple[TestClient, AsyncMock],
) -> None:
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
    ):
        response = client.post(GENERATE_URL, json=GENERATE_BODY)

    assert response.status_code == 200
    marker_statement = session.execute.await_args_list[-1].args[0]
    statement_text = str(marker_statement)
    assert "suggestion_sessions.project_id" in statement_text
    assert "suggestion_sessions.user_id" in statement_text
    assert "suggestion_sessions.branch" in statement_text
    assert "suggestion_sessions.status" in statement_text
    assert marker_statement.compile().params["is_llm_generated"] is True
    session.commit.assert_awaited_once()


def test_generation_without_matching_active_session_is_harmless(
    authed_client: tuple[TestClient, AsyncMock],
) -> None:
    client, session = authed_client
    _happy_path_execute(session, marker_rowcount=0)
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
    ):
        response = client.post(GENERATE_URL, json=GENERATE_BODY)

    assert response.status_code == 200
    session.commit.assert_not_awaited()


@pytest.fixture
def generation_pipeline(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    pipeline = AsyncMock()
    monkeypatch.setattr(
        "ontokit.api.routes.generation.SuggestionGenerationService",
        Mock(return_value=Mock(generate=pipeline)),
    )
    monkeypatch.setattr("ontokit.api.routes.generation._get_redis", lambda: None)
    monkeypatch.setattr(
        "ontokit.api.routes.generation.check_budget", AsyncMock(return_value=(True, None))
    )
    monkeypatch.setattr("ontokit.api.routes.generation.get_provider", Mock())
    return pipeline


@pytest.mark.parametrize(
    "suggestion_type", ["children", "siblings", "annotations", "parents", "edges"]
)
def test_generate_records_only_new_entity_suggestions(
    authed_client: tuple[TestClient, AsyncMock],
    generation_pipeline: AsyncMock,
    ledger_session: AsyncMock,
    suggestion_type: str,
) -> None:
    client, session = authed_client
    _happy_path_execute(session, marker_rowcount=0)
    generated = _generation_response()
    proposal = generated.suggestions[0].model_copy(
        update={"suggestion_type": suggestion_type, "prompt_template": suggestion_type}
    )
    blocked = proposal.model_copy(
        update={
            "iri": "http://example.org/ontology#other",
            "label": "Service Contract",
            "definition": None,
            "duplicate_verdict": "block",
            "validation_errors": [
                ValidationError(field="label", code="invalid_label", message="Review the label.")
            ],
        }
    )
    generated.suggestions = [proposal, blocked]
    generation_pipeline.return_value = generated
    branch = "suggest/test"

    response = client.post(
        GENERATE_URL, json={**GENERATE_BODY, "suggestion_type": suggestion_type, "branch": branch}
    )

    assert response.status_code == 200
    batch_id = UUID(response.json()["batch_id"])
    assert batch_id.version == 4
    session.add_all.assert_not_called()
    session.commit.assert_not_awaited()
    if suggestion_type not in {"children", "siblings"}:
        ledger_session.add_all.assert_not_called()
        ledger_session.commit.assert_not_awaited()
        return

    ledger_session.add_all.assert_called_once()
    rows = ledger_session.add_all.call_args.args[0]
    assert len(rows) == 2
    for row, suggestion in zip(rows, generated.suggestions, strict=True):
        assert row.project_id == UUID(PROJECT_ID)
        assert row.branch == branch
        assert row.entity_iri == suggestion.iri
        assert row.batch_id == batch_id
        assert row.suggestion_type == suggestion_type
        assert row.focus_iri == GENERATE_BODY["class_iri"]
        assert row.label == suggestion.label
        assert row.definition == suggestion.definition
        assert row.model == suggestion.model
        assert row.prompt_template == suggestion.prompt_template
        assert row.user_id == "test-user-id"
    ledger_session.commit.assert_awaited_once()


def test_generate_uses_a_new_batch_id_for_each_request(
    authed_client: tuple[TestClient, AsyncMock], generation_pipeline: AsyncMock
) -> None:
    client, session = authed_client
    generation_pipeline.side_effect = lambda **_kwargs: _generation_response()
    _happy_path_execute(session)
    first = client.post(GENERATE_URL, json=GENERATE_BODY)
    _happy_path_execute(session)
    second = client.post(GENERATE_URL, json=GENERATE_BODY)

    assert first.status_code == second.status_code == 200
    assert UUID(first.json()["batch_id"]) != UUID(second.json()["batch_id"])


def test_ledger_is_durable_before_the_response_and_its_failure_keeps_the_suggestions(
    authed_client: tuple[TestClient, AsyncMock],
    generation_pipeline: AsyncMock,
    ledger_session: AsyncMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An acceptance can only follow the response, so it always finds the ledger rows."""
    _, session = authed_client
    generated = _generation_response()
    generation_pipeline.side_effect = lambda **_kwargs: generated
    label = generated.suggestions[0].label
    operations = []

    async def commit() -> None:
        operations.append("ledger")

    async def fail_commit() -> None:
        operations.append("ledger")
        raise RuntimeError(label)

    async def observe_response(scope: Scope, receive: Receive, send: Send) -> None:
        async def record_body(message: Message) -> None:
            if message["type"] == "http.response.start":
                operations.append("response")
            await send(message)

        await asyncio.wait_for(app(scope, receive, record_body), timeout=2)

    client = TestClient(observe_response)
    for ledger_commit in (commit, fail_commit):
        operations.clear()
        _happy_path_execute(session)
        ledger_session.commit.side_effect = ledger_commit
        response = client.post(GENERATE_URL, json=GENERATE_BODY)

        assert response.status_code == 200
        assert UUID(response.json()["batch_id"]).version == 4
        assert response.json()["suggestions"][0]["label"] == label
        assert operations == ["ledger", "response"]

    session.rollback.assert_not_awaited()
    bind = generation.async_session_maker.call_args.kwargs.get("bind", engine)
    assert bind.sync_engine.hide_parameters
    assert bind.pool is engine.pool
    assert label not in caplog.text
    assert any(
        "ledger" in record.getMessage() and record.levelno == logging.WARNING
        for record in caplog.records
    )


def test_stalled_ledger_write_does_not_hold_the_response(
    authed_client: tuple[TestClient, AsyncMock],
    generation_pipeline: AsyncMock,
    ledger_session: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, session = authed_client
    _happy_path_execute(session)
    generation_pipeline.return_value = _generation_response()

    async def stall() -> None:
        await asyncio.Event().wait()

    ledger_session.commit.side_effect = stall
    monkeypatch.setattr(generation, "_LEDGER_WRITE_TIMEOUT_SECONDS", 0.01, raising=False)

    response = client.post(GENERATE_URL, json=GENERATE_BODY)

    assert response.status_code == 200
    assert "TimeoutError" in caplog.text


def test_generate_transient_failure_returns_batch_id(
    authed_client: tuple[TestClient, AsyncMock],
    generation_pipeline: AsyncMock,
    ledger_session: AsyncMock,
) -> None:
    client, session = authed_client
    _happy_path_execute(session)
    generation_pipeline.side_effect = TimeoutError

    response = client.post(GENERATE_URL, json=GENERATE_BODY)

    assert response.status_code == 200
    assert UUID(response.json()["batch_id"]).version == 4
    assert response.json()["suggestions"] == []
    ledger_session.add_all.assert_not_called()
    ledger_session.commit.assert_not_awaited()


def test_generate_402_when_atomic_reservation_refuses_call(
    authed_client: tuple[TestClient, AsyncMock],
):
    """A concurrent request that consumes the remaining cap still blocks actuation."""
    client, session = authed_client
    _happy_path_execute(session)

    svc_instance = MagicMock()
    svc_instance.generate = AsyncMock(side_effect=LLMBudgetExceeded("daily_cap_reached"))

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
    ):
        resp = client.post(GENERATE_URL, json=GENERATE_BODY)

    assert resp.status_code == 402
    assert "daily" in resp.json()["detail"].lower()


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
