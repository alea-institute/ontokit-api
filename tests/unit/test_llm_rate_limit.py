"""Tests for LLM rate limiting — COST-03, COST-04."""
import pytest


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 02")
def test_editor_rate_limit():
    """Editor is rate-limited to 500 calls/day per project."""


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 02")
def test_suggester_rate_limit():
    """Suggester is rate-limited to 100 calls/day per project."""
