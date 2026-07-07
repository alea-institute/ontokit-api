"""Tests for LLM audit logging schema — LLM-07.

The audit trail is metadata-only: it records who/when/how-much, never the
prompt or model response content (which for suggestions could echo ontology
data, and must not become a second copy of user secrets or content).
"""

from ontokit.models.llm_config import LLMAuditLog
from ontokit.schemas.llm import LLMAuditEntry, LLMUserUsage

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
