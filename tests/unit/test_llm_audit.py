"""Tests for LLM audit logging schema and spend reservations — LLM-07.

The audit trail is metadata-only: it records who/when/how-much, never the
prompt or model response content (which for suggestions could echo ontology
data, and must not become a second copy of user secrets or content).
"""

import uuid
from unittest.mock import AsyncMock, Mock, patch

import pytest

from ontokit.models.llm_config import LLMAuditLog
from ontokit.schemas.llm import LLMAuditEntry, LLMUserUsage
from ontokit.services.llm.audit import finalize_llm_call, reserve_llm_call

# Substrings that would indicate prompt/response content leaking into the audit trail.
_CONTENT_MARKERS = ("prompt", "response", "content", "message", "completion", "text", "api_key")


def test_audit_entry_schema_is_metadata_only():
    """LLMAuditEntry exposes only usage metadata — no prompt/response content."""
    fields = set(LLMAuditEntry.model_fields)

    # Positive: the metadata we expect.
    assert {"user_id", "model", "provider", "input_tokens", "output_tokens"} <= fields

    # Negative: no content-bearing field.
    for field in fields:
        assert not any(marker in field.lower() for marker in _CONTENT_MARKERS), (
            f"audit entry field {field!r} looks like it carries prompt/response content"
        )


def test_audit_model_columns_are_metadata_only():
    """The LLMAuditLog ORM model stores no prompt/response content columns."""
    columns = {c.name for c in LLMAuditLog.__table__.columns}

    for name in columns:
        assert not any(marker in name.lower() for marker in _CONTENT_MARKERS), (
            f"audit log column {name!r} looks like it carries prompt/response content"
        )


def test_user_usage_reports_byo_flag_not_key():
    """Per-user usage reports whether a BYO key was used, never the key itself."""
    fields = set(LLMUserUsage.model_fields)

    assert "is_byo_key" in fields
    assert "api_key" not in fields
    assert "key" not in fields


@pytest.mark.asyncio
async def test_reservation_commits_before_returning() -> None:
    db = AsyncMock()
    entry = Mock(id=uuid.uuid4())

    with (
        patch(
            "ontokit.services.llm.audit.lock_and_check_budget",
            new=AsyncMock(return_value=(True, None)),
        ) as budget,
        patch(
            "ontokit.services.llm.audit.log_llm_call",
            new=AsyncMock(return_value=entry),
        ) as log_call,
    ):
        reservation_id, reason = await reserve_llm_call(
            db,
            project_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
            config=Mock(),
            user_id="user-1",
            model="paid-model",
            provider="provider",
            endpoint="embeddings/test",
            input_tokens=4,
            output_tokens=0,
            cost_estimate_usd=0.25,
        )

    assert (reservation_id, reason) == (entry.id, None)
    budget.assert_awaited_once()
    assert log_call.await_args.kwargs["endpoint"] == "embeddings/test:reserved"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_budget_refusal_rolls_back_without_audit_row() -> None:
    db = AsyncMock()

    with (
        patch(
            "ontokit.services.llm.audit.lock_and_check_budget",
            new=AsyncMock(return_value=(False, "budget_exhausted")),
        ),
        patch("ontokit.services.llm.audit.log_llm_call", new=AsyncMock()) as log_call,
    ):
        reservation_id, reason = await reserve_llm_call(
            db,
            project_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
            config=Mock(),
            user_id="user-1",
            model="paid-model",
            provider="provider",
            endpoint="embeddings/test",
            input_tokens=4,
            output_tokens=0,
            cost_estimate_usd=0.25,
        )

    assert (reservation_id, reason) == (None, "budget_exhausted")
    db.rollback.assert_awaited_once()
    log_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_finalize_preserves_failed_outcome_without_error_detail() -> None:
    db = AsyncMock()
    reservation_id = uuid.uuid4()

    await finalize_llm_call(
        db,
        reservation_id,
        "embeddings/test",
        succeeded=False,
    )

    statement = db.execute.await_args.args[0]
    assert statement.compile().params["endpoint"] == "embeddings/test:failed"
    db.commit.assert_awaited_once()
