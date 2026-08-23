"""Real-PostgreSQL coverage for durable distinct-entity decisions."""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.models.distinct_entity_decision import DistinctEntityDecision
from ontokit.models.duplicate_rejection import DuplicateRejection
from ontokit.models.embedding import EntityEmbedding, ProjectEmbeddingConfig
from ontokit.models.project import Project
from ontokit.schemas.duplicate_check import DistinctDecisionMarkRequest
from ontokit.services.duplicate_check_service import DuplicateCheckService


async def _delete_project(db: AsyncSession, project_id: object) -> None:
    await db.execute(delete(Project).where(Project.id == project_id))
    await db.commit()


def _bind_local_provider(service: DuplicateCheckService) -> None:
    provider = AsyncMock()
    provider.provider_name = "local"
    provider.model_id = "distinct-decision-vector"
    provider.dimensions = 3
    provider.embed_text.return_value = [1.0, 0.0, 0.0]
    service._embedding_svc._get_provider = AsyncMock(return_value=provider)  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_distinct_decision_lifecycle_is_idempotent_and_fingerprint_bound(
    real_db_session: AsyncSession,
) -> None:
    project_id = uuid4()
    candidate_iri = "https://example.test/LegalEntity"
    proposed_iri = "https://example.test/ProposedLegalEntity"
    project = Project(id=project_id, name="Distinct decisions", owner_id="owner-1")
    config = ProjectEmbeddingConfig(
        project_id=project_id,
        provider="local",
        model_name="distinct-decision-vector",
        dimensions=3,
    )
    embedding = EntityEmbedding(
        project_id=project_id,
        branch="main",
        entity_iri=candidate_iri,
        entity_type="class",
        label="Legal Entity",
        embedding_text="Legal Entity",
        embedding=[1.0, 0.0, 0.0],
        dimensions=3,
        provider="local",
        model_name="distinct-decision-vector",
    )
    real_db_session.add(project)
    await real_db_session.flush()
    real_db_session.add_all([config, embedding])
    await real_db_session.commit()
    service = DuplicateCheckService(real_db_session)
    _bind_local_provider(service)
    initial_request = DistinctDecisionMarkRequest(
        proposed_iri=proposed_iri,
        label="Legal Entity",
        candidate_iri=candidate_iri,
        reason="The proposal has a narrower legal meaning.",
    )

    try:
        first = await service.mark_distinct(project_id, initial_request, "editor-1", "editor-1")
        repeated = await service.mark_distinct(project_id, initial_request, "editor-1", "editor-1")
        assert repeated.id == first.id

        suppressed = await service.check(
            project_id,
            initial_request.label,
            proposed_iri=proposed_iri,
            billing_user_id="editor-1",
        )
        assert suppressed.verdict == "pass"
        assert suppressed.candidates == []
        assert [decision.id for decision in suppressed.suppressed_decisions] == [first.id]

        changed_request = initial_request.model_copy(
            update={
                "label": "Materially Changed Legal Entity",
                "reason": "The revised definition is still intentionally distinct.",
            }
        )
        resurfaced = await service.check(
            project_id,
            changed_request.label,
            proposed_iri=proposed_iri,
            billing_user_id="editor-1",
        )
        assert resurfaced.verdict == "block"
        assert [candidate.iri for candidate in resurfaced.candidates] == [candidate_iri]

        successor = await service.mark_distinct(project_id, changed_request, "editor-1", "editor-1")
        await real_db_session.refresh(first)
        assert successor.id != first.id
        assert first.revoked_by == "editor-1"
        assert first.superseded_by_id == successor.id

        history = await service.list_distinct_decisions(project_id, include_inactive=True)
        assert {decision.id for decision in history} == {first.id, successor.id}

        revoked = await service.revoke_distinct_decision(project_id, successor.id, "admin-1")
        assert revoked is not None
        assert revoked.revoked_by == "admin-1"
        revoked_at = revoked.revoked_at
        repeated_revoke = await service.revoke_distinct_decision(
            project_id, successor.id, "admin-2"
        )
        assert repeated_revoke is not None
        assert repeated_revoke.revoked_at == revoked_at
        assert repeated_revoke.revoked_by == "admin-1"
        assert await service.list_distinct_decisions(project_id) == []
    finally:
        await real_db_session.rollback()
        await _delete_project(real_db_session, project_id)


@pytest.mark.asyncio
async def test_active_pair_unique_index_rejects_duplicate_rows(
    real_db_session: AsyncSession,
) -> None:
    project_id = uuid4()
    project = Project(id=project_id, name="Distinct uniqueness", owner_id="owner-1")
    real_db_session.add(project)
    await real_db_session.commit()
    common = {
        "project_id": project_id,
        "iri_a": "https://example.test/A",
        "iri_b": "https://example.test/B",
        "fingerprint_a": "a" * 64,
        "fingerprint_b": "b" * 64,
        "reason": "Distinct by domain expert review.",
        "marked_by": "editor-1",
    }
    try:
        real_db_session.add(DistinctEntityDecision(**common))
        await real_db_session.commit()
        real_db_session.add(DistinctEntityDecision(**common))
        with pytest.raises(IntegrityError):
            await real_db_session.flush()
    finally:
        await real_db_session.rollback()
        await _delete_project(real_db_session, project_id)


def test_pair_identity_is_canonical_when_detector_roles_are_reversed() -> None:
    forward = DuplicateCheckService._fingerprints_for_pair(
        proposed_iri="https://example.test/B",
        proposed_label="Entity B",
        entity_type="class",
        parent_iri=None,
        candidate_iri="https://example.test/A",
        candidate_label="Entity A",
        candidate_entity_type="class",
        candidate_branch="main",
        candidate_embedding_text="Entity A",
        structural_score=None,
    )
    reverse = DuplicateCheckService._fingerprints_for_pair(
        proposed_iri="https://example.test/A",
        proposed_label="Entity A",
        entity_type="class",
        parent_iri=None,
        candidate_iri="https://example.test/B",
        candidate_label="Entity B",
        candidate_entity_type="class",
        candidate_branch="main",
        candidate_embedding_text="Entity B",
        structural_score=None,
    )
    assert forward[:2] == reverse[:2] == (
        "https://example.test/A",
        "https://example.test/B",
    )


@pytest.mark.asyncio
async def test_legacy_rejections_and_distinct_decisions_use_separate_tables(
    real_db_session: AsyncSession,
) -> None:
    """The head migration preserves legacy provenance while adding the audit table."""
    project_id = uuid4()
    project = Project(id=project_id, name="Separate review records", owner_id="owner-1")
    rejection = DuplicateRejection(
        project_id=project_id,
        rejected_iri="https://example.test/Rejected",
        canonical_iri="https://example.test/Canonical",
        rejection_reason="Rejected suggestion provenance stays readable.",
        rejected_by="reviewer-1",
    )
    decision = DistinctEntityDecision(
        project_id=project_id,
        iri_a="https://example.test/A",
        iri_b="https://example.test/B",
        fingerprint_a="a" * 64,
        fingerprint_b="b" * 64,
        reason="Explicitly distinct concepts.",
        marked_by="reviewer-2",
    )
    try:
        real_db_session.add(project)
        await real_db_session.flush()
        real_db_session.add_all([rejection, decision])
        await real_db_session.commit()

        stored_rejection = (
            await real_db_session.execute(
                select(DuplicateRejection).where(DuplicateRejection.id == rejection.id)
            )
        ).scalar_one()
        stored_decision = (
            await real_db_session.execute(
                select(DistinctEntityDecision).where(DistinctEntityDecision.id == decision.id)
            )
        ).scalar_one()
        assert stored_rejection.rejection_reason == rejection.rejection_reason
        assert stored_decision.reason == decision.reason
        assert stored_rejection.__tablename__ == "duplicate_rejections"
        assert stored_decision.__tablename__ == "distinct_entity_decisions"
    finally:
        await real_db_session.rollback()
        await _delete_project(real_db_session, project_id)
