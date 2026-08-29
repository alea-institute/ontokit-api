"""Focused unit coverage for auditable distinct-entity decisions."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from ontokit.models.distinct_entity_decision import DistinctEntityDecision
from ontokit.schemas.duplicate_check import (
    DistinctDecisionMarkRequest,
    DistinctDecisionResponse,
)
from ontokit.schemas.embeddings import SemanticSearchResultWithBranch
from ontokit.services.duplicate_check_service import (
    DuplicateCandidateUnavailableError,
    DuplicateCheckService,
)

PROJECT_ID = uuid4()
PROPOSED_IRI = "https://example.test/ProposedLegalEntity"
CANDIDATE_IRI = "https://example.test/LegalEntity"


def _candidate(
    *,
    iri: str = CANDIDATE_IRI,
    label: str = "Legal Entity",
    branch: str = "main",
    embedding_text: str = "Legal Entity",
    score: float = 1.0,
) -> SemanticSearchResultWithBranch:
    return SemanticSearchResultWithBranch(
        iri=iri,
        label=label,
        entity_type="class",
        score=score,
        deprecated=False,
        branch=branch,
        embedding_text=embedding_text,
    )


def _decision_for(
    service: DuplicateCheckService,
    candidate: SemanticSearchResultWithBranch,
) -> DistinctEntityDecision:
    pair = service._fingerprints_for_pair(
        proposed_iri=PROPOSED_IRI,
        proposed_label="Legal Entity",
        entity_type="class",
        parent_iri=None,
        candidate_iri=candidate.iri,
        candidate_label=candidate.label,
        candidate_entity_type=candidate.entity_type,
        candidate_branch=candidate.branch,
        candidate_embedding_text=candidate.embedding_text,
        structural_score=None,
    )
    return DistinctEntityDecision(
        id=uuid4(),
        project_id=PROJECT_ID,
        iri_a=pair[0],
        iri_b=pair[1],
        fingerprint_a=pair[2],
        fingerprint_b=pair[3],
        reason="These are distinct concepts in this ontology.",
        marked_by="editor-1",
        marked_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_matching_decision_suppresses_and_backfills_top_k() -> None:
    db = MagicMock()
    service = DuplicateCheckService(db)
    suppressed = _candidate()
    visible = _candidate(
        iri="https://example.test/RelatedEntity",
        label="Related Entity",
        score=0.9,
    )
    decision = _decision_for(service, suppressed)
    search = AsyncMock(return_value=[suppressed, visible])

    with (
        patch.object(service._embedding_svc, "semantic_search_all_branches", new=search),
        patch.object(service, "_classify_source", new=AsyncMock(return_value="main")),
        patch.object(
            service,
            "_active_decisions_for_iri",
            new=AsyncMock(return_value={(decision.iri_a, decision.iri_b): decision}),
        ),
    ):
        response = await service.check(
            PROJECT_ID,
            "Legal Entity",
            proposed_iri=PROPOSED_IRI,
            limit=1,
            billing_user_id="editor-1",
        )

    assert search.await_args.kwargs["limit"] == 2
    assert [item.iri for item in response.candidates] == [visible.iri]
    assert [item.id for item in response.suppressed_decisions] == [decision.id]


@pytest.mark.asyncio
async def test_changed_candidate_fingerprint_resurfaces_warning() -> None:
    db = MagicMock()
    service = DuplicateCheckService(db)
    original = _candidate()
    changed = _candidate(embedding_text="Legal Entity with a changed definition")
    stale_decision = _decision_for(service, original)

    with (
        patch.object(
            service._embedding_svc,
            "semantic_search_all_branches",
            new=AsyncMock(return_value=[changed]),
        ),
        patch.object(service, "_classify_source", new=AsyncMock(return_value="main")),
        patch.object(
            service,
            "_active_decisions_for_iri",
            new=AsyncMock(
                return_value={(stale_decision.iri_a, stale_decision.iri_b): stale_decision}
            ),
        ),
    ):
        response = await service.check(
            PROJECT_ID,
            "Legal Entity",
            proposed_iri=PROPOSED_IRI,
            billing_user_id="editor-1",
        )

    assert response.verdict == "block"
    assert [item.iri for item in response.candidates] == [CANDIDATE_IRI]
    assert response.suppressed_decisions == []


@pytest.mark.asyncio
async def test_mark_distinct_recomputes_candidate_and_rejects_stale_request() -> None:
    db = MagicMock()
    service = DuplicateCheckService(db)
    request = DistinctDecisionMarkRequest(
        proposed_iri=PROPOSED_IRI,
        label="Legal Entity",
        candidate_iri=CANDIDATE_IRI,
        candidate_branch="main",
        reason="A narrower meaning was reviewed.",
    )
    with (
        patch.object(service, "_validate_suggestion_session", new=AsyncMock()),
        patch.object(
            service,
            "check",
            new=AsyncMock(
                return_value=MagicMock(candidates=[], suppressed_decisions=[], verdict="pass")
            ),
        ) as check,
        pytest.raises(DuplicateCandidateUnavailableError),
    ):
        await service.mark_distinct(
            PROJECT_ID,
            request,
            actor_id="editor-1",
            billing_user_id="editor-1",
        )

    assert check.await_args.kwargs["suppress_distinct"] is False
    assert check.await_args.kwargs["proposed_iri"] == PROPOSED_IRI
    assert check.await_args.kwargs["billing_user_id"] == "editor-1"


def test_pair_identity_is_canonical_when_detector_roles_reverse() -> None:
    common = {
        "entity_type": "class",
        "parent_iri": None,
        "candidate_branch": "main",
        "structural_score": None,
    }
    forward = DuplicateCheckService._fingerprints_for_pair(
        proposed_iri="https://example.test/B",
        proposed_label="Entity B",
        candidate_iri="https://example.test/A",
        candidate_label="Entity A",
        candidate_entity_type="class",
        candidate_embedding_text="Entity A",
        **common,
    )
    reverse = DuplicateCheckService._fingerprints_for_pair(
        proposed_iri="https://example.test/A",
        proposed_label="Entity A",
        candidate_iri="https://example.test/B",
        candidate_label="Entity B",
        candidate_entity_type="class",
        candidate_embedding_text="Entity B",
        **common,
    )

    assert forward[:2] == reverse[:2] == (
        "https://example.test/A",
        "https://example.test/B",
    )


def test_internal_fingerprints_are_not_serialized_to_candidate_payloads() -> None:
    from ontokit.schemas.duplicate_check import DuplicateCandidate

    candidate = DuplicateCandidate(
        iri=CANDIDATE_IRI,
        label="Legal Entity",
        entity_type="class",
        score=1.0,
        source="main",
        decision_iri_a=PROPOSED_IRI,
        decision_iri_b=CANDIDATE_IRI,
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


def test_decision_response_preserves_audit_fields() -> None:
    service = DuplicateCheckService(MagicMock())
    decision = _decision_for(service, _candidate())
    response = DistinctDecisionResponse.model_validate(decision)
    assert response.reason == decision.reason
    assert response.marked_by == "editor-1"
    assert response.fingerprint_a == decision.fingerprint_a
