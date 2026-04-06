"""Tests for LLM role-based access — ROLE-01 through ROLE-05."""
import pytest


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 02")
def test_anonymous_blocked():
    """Anonymous users get 403 on LLM endpoints."""


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 02")
def test_viewer_blocked():
    """Viewer role has no LLM access."""


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 02")
def test_admin_unlimited():
    """Admin/owner have unlimited rate limits."""


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 02")
def test_editor_access():
    """Editor has LLM access with 500/day limit."""


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 02")
def test_suggester_access():
    """Suggester has LLM access with 100/day limit."""
