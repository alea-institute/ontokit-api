"""Real-PostgreSQL coverage for durable distinct-entity decisions."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.models.distinct_entity_decision import DistinctEntityDecision
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
        repeated = await service.mark_distinct(
            project_id, initial_request, "editor-1", "editor-1"
        )
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

        successor = await service.mark_distinct(
            project_id, changed_request, "editor-1", "editor-1"
        )
        await real_db_session.refresh(first)
        assert successor.id != first.id
        assert first.revoked_by == "editor-1"
        assert first.superseded_by_id == successor.id

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
