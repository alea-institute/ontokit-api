"""Unit tests for duplicate scoring and distinct-decision suppression."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from ontokit.models.distinct_entity_decision import DistinctEntityDecision
from ontokit.models.duplicate_rejection import DuplicateRejection
from ontokit.schemas.duplicate_check import (
    DistinctDecisionResponse,
    DuplicateCandidate,
    DuplicateCheckResponse,
    ScoreBreakdown,
)
from ontokit.schemas.embeddings import SemanticSearchResultWithBranch
from ontokit.services.duplicate_check_service import (
    BLOCK_THRESHOLD,
    SEMANTIC_WEIGHT,
    STRUCTURAL_WEIGHT,
    WARN_THRESHOLD,
    DuplicateCheckService,
)

PROJECT_ID = uuid4()


def _make_sem_result(
    iri: str = "http://example.org/LegalEntity",
    label: str = "Legal Entity",
    score: float = 1.0,
    branch: str = "main",
    embedding_text: str | None = None,
) -> SemanticSearchResultWithBranch:
    return SemanticSearchResultWithBranch(
        iri=iri,
        label=label,
        entity_type="class",
        score=score,
        deprecated=False,
        branch=branch,
        embedding_text=embedding_text or label,
    )


def _make_service() -> tuple[DuplicateCheckService, MagicMock]:
    """Return a DuplicateCheckService with a mocked AsyncSession."""
    db = MagicMock()
    svc = DuplicateCheckService(db)
    return svc, db


@pytest.mark.asyncio
async def test_exact_label_match_returns_block_verdict():
    """Composite score > 0.95 produces verdict='block' — submission is rejected (DEDUP-05)."""
    svc, _ = _make_service()

    # exact=1.0 (label matches), semantic=1.0, structural=1.0 → composite=1.0 → block
    sem_result = _make_sem_result(label="Legal Entity", score=1.0, branch="main")

    with (
        patch.object(
            svc._embedding_svc,
            "semantic_search_all_branches",
            new=AsyncMock(return_value=[sem_result]),
        ),
        patch.object(
            svc._structural_svc,
            "try_compute_similarity",
            return_value=1.0,
        ),
        patch.object(
            svc,
            "_classify_source",
            new=AsyncMock(return_value="main"),
        ),
    ):
        response = await svc.check(
            project_id=PROJECT_ID,
            label="Legal Entity",  # exact match
            parent_iri="http://example.org/Entity",  # enables structural score
        )

    assert response.verdict == "block"
    assert response.composite_score > BLOCK_THRESHOLD


@pytest.mark.asyncio
async def test_exact_label_blocks_even_when_other_signals_are_weaker():
    """An exact normalized label is deterministic duplicate evidence."""
    svc, _ = _make_service()

    sem_result = _make_sem_result(label="Legal Entity", score=0.8, branch="main")

    with (
        patch.object(
            svc._embedding_svc,
            "semantic_search_all_branches",
            new=AsyncMock(return_value=[sem_result]),
        ),
        patch.object(
            svc._structural_svc,
            "try_compute_similarity",
            return_value=0.5,
        ),
        patch.object(
            svc,
            "_classify_source",
            new=AsyncMock(return_value="main"),
        ),
    ):
        response = await svc.check(
            project_id=PROJECT_ID,
            label="Legal Entity",
            parent_iri="http://example.org/Entity",
        )

    assert response.verdict == "block"
    assert response.composite_score == 1.0


@pytest.mark.asyncio
async def test_below_threshold_passes_silently():
    """Composite score <= 0.80 produces verdict='pass' — no user friction (DEDUP-07)."""
    svc, _ = _make_service()

    # exact=0.0 (different label), semantic=0.5, structural=0.3 → 0+0.2+0.06 = 0.26 → pass
    sem_result = _make_sem_result(label="Completely Different Concept", score=0.5, branch="main")

    with (
        patch.object(
            svc._embedding_svc,
            "semantic_search_all_branches",
            new=AsyncMock(return_value=[sem_result]),
        ),
        patch.object(
            svc._structural_svc,
            "try_compute_similarity",
            return_value=0.3,
        ),
        patch.object(
            svc,
            "_classify_source",
            new=AsyncMock(return_value="main"),
        ),
    ):
        response = await svc.check(
            project_id=PROJECT_ID,
            label="My New Concept",  # different label → exact_score=0.0
            parent_iri="http://example.org/Entity",
        )

    assert response.verdict == "pass"
    assert response.composite_score <= WARN_THRESHOLD


@pytest.mark.asyncio
async def test_composite_score_weights():
    """Composite = 0.40 * exact + 0.40 * semantic + 0.20 * structural (DEDUP-04, D-01)."""
    svc, _ = _make_service()

    # exact=0.0, semantic=0.5, structural=0.75
    sem_result = _make_sem_result(label="Related Label", score=0.5, branch="main")

    with (
        patch.object(
            svc._embedding_svc,
            "semantic_search_all_branches",
            new=AsyncMock(return_value=[sem_result]),
        ),
        patch.object(
            svc._structural_svc,
            "try_compute_similarity",
            return_value=0.75,
        ),
        patch.object(
            svc,
            "_classify_source",
            new=AsyncMock(return_value="main"),
        ),
    ):
        response = await svc.check(
            project_id=PROJECT_ID,
            label="Target Label",  # exact match → exact_score=1.0
            parent_iri="http://example.org/Entity",
        )

    expected_composite = round(SEMANTIC_WEIGHT * 0.5 + STRUCTURAL_WEIGHT * 0.75, 4)
    assert response.composite_score == expected_composite
    assert response.score_breakdown.exact == 0.0
    assert response.score_breakdown.semantic == 0.5
    assert response.score_breakdown.structural == 0.75


@pytest.mark.asyncio
async def test_missing_structural_signal_renormalizes_available_weights():
    """A minted IRI with no parent can still block on exact + semantic identity."""
    svc, _ = _make_service()
    sem_result = _make_sem_result(label="Legal Entity", score=0.9, branch="main")

    with (
        patch.object(
            svc._embedding_svc,
            "semantic_search_all_branches",
            new=AsyncMock(return_value=[sem_result]),
        ),
        patch.object(svc, "_classify_source", new=AsyncMock(return_value="main")),
    ):
        response = await svc.check(
            project_id=PROJECT_ID,
            label="Legal Entity",
            parent_iri=None,
        )

    assert response.composite_score == 1.0
    assert response.verdict == "block"


@pytest.mark.asyncio
async def test_semantic_only_near_duplicate_can_warn():
    svc, _ = _make_service()
    sem_result = _make_sem_result(label="Legal Organization", score=0.86, branch="main")
    with (
        patch.object(
            svc._embedding_svc,
            "semantic_search_all_branches",
            new=AsyncMock(return_value=[sem_result]),
        ),
        patch.object(svc, "_classify_source", new=AsyncMock(return_value="main")),
    ):
        response = await svc.check(PROJECT_ID, "Legal Entity", parent_iri=None)
    assert response.verdict == "warn"
    assert response.composite_score == 0.86


@pytest.mark.asyncio
async def test_all_branch_scope():
    """Duplicate search spans all project branches, not just the active one (DEDUP-08)."""
    svc, _ = _make_service()

    # Candidates from three different branches
    candidates = [
        _make_sem_result(iri="http://ex.org/A", label="Concept A", score=0.9, branch="main"),
        _make_sem_result(
            iri="http://ex.org/B", label="Concept B", score=0.85, branch="suggest-123"
        ),
        _make_sem_result(iri="http://ex.org/C", label="Concept C", score=0.8, branch="suggest-456"),
    ]

    with (
        patch.object(
            svc._embedding_svc,
            "semantic_search_all_branches",
            new=AsyncMock(return_value=candidates),
        ),
        patch.object(
            svc._structural_svc,
            "try_compute_similarity",
            return_value=0.0,
        ),
        patch.object(
            svc,
            "_classify_source",
            new=AsyncMock(return_value="main"),
        ),
    ):
        response = await svc.check(
            project_id=PROJECT_ID,
            label="Any Label",
        )

    response_branches = {c.branch for c in response.candidates}
    assert "main" in response_branches
    assert "suggest-123" in response_branches
    assert "suggest-456" in response_branches
    assert len(response.candidates) == 3


@pytest.mark.asyncio
async def test_rejected_suggestion_restores_legacy_rejection_provenance():
    """Rejected branch candidates retain their legacy review explanation."""
    svc, _ = _make_service()

    sem_result = _make_sem_result(
        iri="http://ex.org/RejectedEntity",
        label="Some Duplicate Label",
        score=0.95,
        branch="suggest-old",
    )

    with (
        patch.object(
            svc._embedding_svc,
            "semantic_search_all_branches",
            new=AsyncMock(return_value=[sem_result]),
        ),
        patch.object(
            svc._structural_svc,
            "try_compute_similarity",
            return_value=0.0,
        ),
        patch.object(svc, "_classify_source", new=AsyncMock(return_value="rejected")),
        patch.object(
            svc,
            "_get_rejection_info",
            new=AsyncMock(
                return_value=DuplicateRejection(
                    project_id=PROJECT_ID,
                    rejected_iri=sem_result.iri,
                    canonical_iri="http://ex.org/CanonicalEntity",
                    rejection_reason="The suggestion used the wrong jurisdiction.",
                    rejected_by="reviewer-1",
                )
            ),
        ),
    ):
        response = await svc.check(
            project_id=PROJECT_ID,
            label="Some Other Label",
        )

    assert len(response.candidates) == 1
    candidate = response.candidates[0]
    assert candidate.source == "rejected"
    assert candidate.rejection_reason == "The suggestion used the wrong jurisdiction."
    assert candidate.canonical_iri == "http://ex.org/CanonicalEntity"


@pytest.mark.asyncio
async def test_response_includes_score_breakdown():
    """Response payload contains verdict, composite_score, score_breakdown, and candidates (D-13)."""
    svc, _ = _make_service()

    sem_result = _make_sem_result(score=0.7, branch="main")

    with (
        patch.object(
            svc._embedding_svc,
            "semantic_search_all_branches",
            new=AsyncMock(return_value=[sem_result]),
        ),
        patch.object(
            svc._structural_svc,
            "try_compute_similarity",
            return_value=0.5,
        ),
        patch.object(
            svc,
            "_classify_source",
            new=AsyncMock(return_value="main"),
        ),
    ):
        response = await svc.check(
            project_id=PROJECT_ID,
            label="Test Label",
            parent_iri="http://example.org/Parent",
        )

    # Check response shape (D-13)
    assert isinstance(response, DuplicateCheckResponse)
    assert response.verdict in ("block", "warn", "pass")
    assert isinstance(response.composite_score, float)
    assert isinstance(response.score_breakdown, ScoreBreakdown)
    assert isinstance(response.score_breakdown.exact, float)
    assert isinstance(response.score_breakdown.semantic, float)
    assert isinstance(response.score_breakdown.structural, float)
    assert isinstance(response.candidates, list)
    assert len(response.candidates) == 1

    candidate = response.candidates[0]
    assert isinstance(candidate, DuplicateCandidate)
    assert isinstance(candidate.iri, str)
    assert isinstance(candidate.label, str)
    assert isinstance(candidate.score, float)
    assert candidate.source in ("main", "pending", "rejected")


@pytest.mark.asyncio
async def test_active_distinct_decision_suppresses_candidate_before_verdict():
    """A matching fingerprint-bound decision removes the candidate from scoring."""
    svc, _ = _make_service()
    sem_result = _make_sem_result(label="Legal Entity", score=1.0, branch="main")
    pair = svc._fingerprints_for_pair(
        proposed_iri="http://example.org/ProposedLegalEntity",
        proposed_label="Legal Entity",
        entity_type="class",
        parent_iri=None,
        candidate_iri=sem_result.iri,
        candidate_label=sem_result.label,
        candidate_entity_type=sem_result.entity_type,
        candidate_branch=sem_result.branch,
        candidate_embedding_text=sem_result.embedding_text,
        structural_score=None,
    )
    decision = DistinctEntityDecision(
        id=uuid4(),
        project_id=PROJECT_ID,
        iri_a=pair[0],
        iri_b=pair[1],
        fingerprint_a=pair[2],
        fingerprint_b=pair[3],
        reason="These are distinct concepts in this ontology.",
        marked_by="reviewer-1",
        marked_at=datetime.now(UTC),
    )

    with (
        patch.object(
            svc._embedding_svc,
            "semantic_search_all_branches",
            new=AsyncMock(return_value=[sem_result]),
        ),
        patch.object(svc, "_classify_source", new=AsyncMock(return_value="main")),
        patch.object(
            svc,
            "_active_decisions_for_iri",
            new=AsyncMock(return_value={(pair[0], pair[1]): decision}),
        ),
    ):
        response = await svc.check(
            PROJECT_ID,
            "Legal Entity",
            proposed_iri="http://example.org/ProposedLegalEntity",
        )

    assert response.verdict == "pass"
    assert response.composite_score == 0.0
    assert response.candidates == []
    assert response.suppressed_decisions == [DistinctDecisionResponse.model_validate(decision)]


@pytest.mark.asyncio
async def test_stale_distinct_decision_does_not_suppress_changed_input():
    """Materially changed normalized inputs resurface the candidate."""
    svc, _ = _make_service()
    sem_result = _make_sem_result(label="Legal Entity", score=1.0, branch="main")
    pair = svc._fingerprints_for_pair(
        proposed_iri="http://example.org/ProposedLegalEntity",
        proposed_label="Materially changed legal entity",
        entity_type="class",
        parent_iri=None,
        candidate_iri=sem_result.iri,
        candidate_label=sem_result.label,
        candidate_entity_type=sem_result.entity_type,
        candidate_branch=sem_result.branch,
        candidate_embedding_text=sem_result.embedding_text,
        structural_score=None,
    )
    stale_decision = DistinctEntityDecision(
        id=uuid4(),
        project_id=PROJECT_ID,
        iri_a=pair[0],
        iri_b=pair[1],
        fingerprint_a="a" * 64,
        fingerprint_b="b" * 64,
        reason="Decision for prior inputs",
        marked_by="reviewer-1",
        marked_at=datetime.now(UTC),
    )

    with (
        patch.object(
            svc._embedding_svc,
            "semantic_search_all_branches",
            new=AsyncMock(return_value=[sem_result]),
        ),
        patch.object(svc, "_classify_source", new=AsyncMock(return_value="main")),
        patch.object(
            svc,
            "_active_decisions_for_iri",
            new=AsyncMock(return_value={(pair[0], pair[1]): stale_decision}),
        ) as find_decisions,
    ):
        response = await svc.check(
            PROJECT_ID,
            "Materially changed legal entity",
            proposed_iri="http://example.org/ProposedLegalEntity",
        )

    assert find_decisions.await_count == 1
    assert response.verdict == "block"
    assert [candidate.iri for candidate in response.candidates] == [sem_result.iri]


@pytest.mark.asyncio
async def test_distinct_fingerprint_is_scoped_to_candidate_branch_occurrence():
    """The same IRI on another branch is independently evaluated."""
    svc, _ = _make_service()
    reviewed = _make_sem_result(
        branch="suggest/reviewed",
        embedding_text="Legal Entity\nDefinition: reviewed branch",
    )
    changed = _make_sem_result(
        branch="suggest/changed",
        embedding_text="Legal Entity\nDefinition: changed branch",
    )
    pair = svc._fingerprints_for_pair(
        proposed_iri="http://example.org/ProposedLegalEntity",
        proposed_label="Legal Entity",
        entity_type="class",
        parent_iri=None,
        candidate_iri=reviewed.iri,
        candidate_label=reviewed.label,
        candidate_entity_type=reviewed.entity_type,
        candidate_branch=reviewed.branch,
        candidate_embedding_text=reviewed.embedding_text,
        structural_score=None,
    )
    decision = DistinctEntityDecision(
        id=uuid4(),
        project_id=PROJECT_ID,
        iri_a=pair[0],
        iri_b=pair[1],
        fingerprint_a=pair[2],
        fingerprint_b=pair[3],
        reason="Only the reviewed branch occurrence is distinct.",
        marked_by="reviewer-1",
        marked_at=datetime.now(UTC),
    )

    with (
        patch.object(
            svc._embedding_svc,
            "semantic_search_all_branches",
            new=AsyncMock(return_value=[reviewed, changed]),
        ),
        patch.object(svc, "_classify_source", new=AsyncMock(return_value="pending")),
        patch.object(
            svc,
            "_active_decisions_for_iri",
            new=AsyncMock(return_value={(pair[0], pair[1]): decision}),
        ),
    ):
        response = await svc.check(
            PROJECT_ID,
            "Legal Entity",
            proposed_iri="http://example.org/ProposedLegalEntity",
        )

    assert [item.branch for item in response.candidates] == ["suggest/changed"]
    assert [item.id for item in response.suppressed_decisions] == [decision.id]


@pytest.mark.asyncio
async def test_candidate_embedding_text_change_invalidates_distinct_decision():
    svc, _ = _make_service()
    original = _make_sem_result(embedding_text="Legal Entity\nDefinition: original")
    changed = _make_sem_result(embedding_text="Legal Entity\nDefinition: revised")
    pair = svc._fingerprints_for_pair(
        proposed_iri="http://example.org/ProposedLegalEntity",
        proposed_label="Legal Entity",
        entity_type="class",
        parent_iri="http://example.org/Parent",
        candidate_iri=original.iri,
        candidate_label=original.label,
        candidate_entity_type=original.entity_type,
        candidate_branch=original.branch,
        candidate_embedding_text=original.embedding_text,
        structural_score=0.5,
    )
    decision = DistinctEntityDecision(
        id=uuid4(),
        project_id=PROJECT_ID,
        iri_a=pair[0],
        iri_b=pair[1],
        fingerprint_a=pair[2],
        fingerprint_b=pair[3],
        reason="Reviewed before the definition changed.",
        marked_by="reviewer-1",
        marked_at=datetime.now(UTC),
    )

    with (
        patch.object(
            svc._embedding_svc,
            "semantic_search_all_branches",
            new=AsyncMock(return_value=[changed]),
        ),
        patch.object(svc._structural_svc, "try_compute_similarity", return_value=0.5),
        patch.object(svc, "_classify_source", new=AsyncMock(return_value="main")),
        patch.object(
            svc,
            "_active_decisions_for_iri",
            new=AsyncMock(return_value={(pair[0], pair[1]): decision}),
        ),
    ):
        response = await svc.check(
            PROJECT_ID,
            "Legal Entity",
            parent_iri="http://example.org/Parent",
            proposed_iri="http://example.org/ProposedLegalEntity",
        )

    assert response.verdict == "block"
    assert [item.iri for item in response.candidates] == [changed.iri]


def test_structural_detector_change_alters_pair_fingerprint():
    common = {
        "proposed_iri": "http://example.org/ProposedLegalEntity",
        "proposed_label": "Legal Entity",
        "entity_type": "class",
        "parent_iri": "http://example.org/Parent",
        "candidate_iri": "http://example.org/LegalEntity",
        "candidate_label": "Legal Entity",
        "candidate_entity_type": "class",
        "candidate_branch": "main",
        "candidate_embedding_text": "Legal Entity\nDefinition: stable",
    }

    before = DuplicateCheckService._fingerprints_for_pair(**common, structural_score=0.25)
    after = DuplicateCheckService._fingerprints_for_pair(**common, structural_score=0.75)

    assert before[:2] == after[:2]
    assert before[2:] != after[2:]


def test_internal_decision_fingerprints_are_not_serialized_to_api_payloads():
    candidate = DuplicateCandidate(
        iri="http://example.org/A",
        label="A",
        score=0.9,
        source="main",
        decision_iri_a="http://example.org/A",
        decision_iri_b="http://example.org/B",
        decision_fingerprint_a="a" * 64,
        decision_fingerprint_b="b" * 64,
    )

    assert set(candidate.model_dump()) == {
        "iri",
        "label",
        "entity_type",
        "score",
        "source",
        "branch",
        "rejection_reason",
        "canonical_iri",
    }


@pytest.mark.asyncio
async def test_suppressed_top_result_is_backfilled_to_requested_limit():
    svc, _ = _make_service()
    proposed_iri = "http://example.org/Proposed"
    suppressed = _make_sem_result(iri="http://example.org/A", score=0.99)
    visible = _make_sem_result(iri="http://example.org/B", label="Related", score=0.9)
    pair = svc._fingerprints_for_pair(
        proposed_iri=proposed_iri,
        proposed_label="Proposal",
        entity_type="class",
        parent_iri=None,
        candidate_iri=suppressed.iri,
        candidate_label=suppressed.label,
        candidate_entity_type=suppressed.entity_type,
        candidate_branch=suppressed.branch,
        candidate_embedding_text=suppressed.embedding_text,
        structural_score=None,
    )
    decision = DistinctEntityDecision(
        id=uuid4(),
        project_id=PROJECT_ID,
        iri_a=pair[0],
        iri_b=pair[1],
        fingerprint_a=pair[2],
        fingerprint_b=pair[3],
        reason="Reviewed pair",
        marked_by="reviewer-1",
        marked_at=datetime.now(UTC),
    )
    search = AsyncMock(return_value=[suppressed, visible])

    with (
        patch.object(svc._embedding_svc, "semantic_search_all_branches", new=search),
        patch.object(svc, "_classify_source", new=AsyncMock(return_value="main")),
        patch.object(
            svc,
            "_active_decisions_for_iri",
            new=AsyncMock(return_value={(pair[0], pair[1]): decision}),
        ),
    ):
        response = await svc.check(
            PROJECT_ID,
            "Proposal",
            proposed_iri=proposed_iri,
            limit=1,
        )

    assert search.await_args.kwargs["limit"] == 2
    assert [item.iri for item in response.candidates] == [visible.iri]
    assert response.verdict == "warn"
