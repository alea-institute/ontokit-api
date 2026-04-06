"""Tests for LLM budget enforcement — COST-01, COST-02, COST-07."""
import pytest


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 02")
def test_byo_excluded_from_budget():
    """BYO-key calls do not count against project budget."""


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 02")
def test_budget_exhaustion_402():
    """Budget exhaustion returns 402 and blocks further calls."""
