"""Wave 0 stubs for composite duplicate-check scoring — Plan 04 (DEDUP-04 through DEDUP-08)."""
import pytest


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 04")
def test_exact_label_match_returns_block_verdict():
    """Composite score > 0.95 produces verdict='block' — submission is rejected (DEDUP-05)."""


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 04")
def test_semantic_similarity_warn_range():
    """Composite score in [0.80, 0.95] produces verdict='warn' — UI surfaces warning (DEDUP-06)."""


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 04")
def test_below_threshold_passes_silently():
    """Composite score <= 0.80 produces verdict='pass' — no user friction (DEDUP-07)."""


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 04")
def test_composite_score_weights():
    """Composite = 0.40 * exact + 0.40 * semantic + 0.20 * structural (DEDUP-04, D-01)."""


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 04")
def test_all_branch_scope():
    """Duplicate search spans all project branches, not just the active one (DEDUP-08)."""


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 04")
def test_rejection_history_surfaced():
    """Previously-rejected candidates include rejection_reason in the response (D-09)."""


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plan 04")
def test_response_includes_score_breakdown():
    """Response payload contains verdict, composite_score, score_breakdown, and candidates (D-13)."""
