"""Live regression proofs for the 2026-08-08 LLM subsystem review."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.api.routes import generation, semantic_search
from ontokit.core.auth import CurrentUser
from ontokit.git.bare_repository import BareGitRepositoryService
from ontokit.models.embedding import EntityEmbedding, ProjectEmbeddingConfig
from ontokit.models.llm_config import ProjectLLMConfig
from ontokit.models.project import Project, ProjectMember
from ontokit.models.suggestion_outcome import SuggestionOutcome
from ontokit.models.suggestion_session import SuggestionSession, SuggestionSessionStatus
from ontokit.schemas.generation import GenerateSuggestionsRequest
from ontokit.schemas.suggestion import SuggestionSaveRequest
from ontokit.services.duplicate_check_service import DuplicateCheckService
from ontokit.services.llm import pricing
from ontokit.services.suggestion_service import SuggestionService

pytestmark = pytest.mark.integration


async def _delete_project(db: AsyncSession, project_id: UUID) -> None:
    """Remove committed fixture rows after services under test call commit()."""
    await db.rollback()
    await db.execute(delete(Project).where(Project.id == project_id))
    await db.commit()


@pytest.mark.asyncio
async def test_p0_1_suggestion_save_commits_with_real_git_service(
    real_db_session: AsyncSession, tmp_path: Path
) -> None:
    """Suggestion save uses the actual bare-repository service contract."""
    project_id = uuid4()
    user = CurrentUser(id="git-user", name="Git User", email="git@example.test")
    project = Project(id=project_id, name="P0-1", owner_id=user.id, source_file_path="ontology.ttl")
    project.members.append(ProjectMember(user_id=user.id, role="editor"))
    session = SuggestionSession(
        project_id=project_id,
        user_id=user.id,
        user_name=user.name,
        session_id="p0-1-save",
        branch="suggestion/p0-1-save",
        beacon_token="integration-token",
    )
    real_db_session.add_all([project, session])
    await real_db_session.commit()

    git = BareGitRepositoryService(base_path=str(tmp_path))
    git.initialize_repository(
        project_id,
        b"@prefix ex: <https://example.test/> .\n",
        "ontology.ttl",
    )
    git.create_branch(project_id, session.branch, from_ref="main")

    try:
        result = await SuggestionService(real_db_session, git).save(
            project_id,
            session.session_id,
            SuggestionSaveRequest(
                content="@prefix ex: <https://example.test/> .\nex:Thing a ex:Class .\n",
                entity_iri="https://example.test/Thing",
                entity_label="Thing",
            ),
            user,
        )
        assert result.commit_hash
        assert git.get_repository(project_id).read_file(session.branch, "ontology.ttl").endswith(
            b"ex:Thing a ex:Class .\n"
        )
    finally:
        await _delete_project(real_db_session, project_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "review_status",
    [
        SuggestionSessionStatus.MERGED,
        SuggestionSessionStatus.REJECTED,
        SuggestionSessionStatus.CHANGES_REQUESTED,
    ],
)
async def test_p0_2_database_accepts_review_action_statuses(
    real_db_session: AsyncSession, review_status: SuggestionSessionStatus
) -> None:
    """The migrated Postgres CHECK agrees with the application status enum."""
    project_id = uuid4()
    project = Project(id=project_id, name=f"P0-2-{review_status}", owner_id="review-owner")
    session = SuggestionSession(
        project_id=project_id,
        user_id="contributor",
        session_id=f"p0-2-{review_status}",
        branch=f"suggestion/p0-2-{review_status}",
        beacon_token="integration-token",
        status=SuggestionSessionStatus.SUBMITTED.value,
    )
    real_db_session.add_all([project, session])
    await real_db_session.commit()

    try:
        session.status = review_status.value
        await real_db_session.commit()
        await real_db_session.refresh(session)
        assert session.status == review_status.value
    finally:
        await _delete_project(real_db_session, project_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_iri", [None, "https://example.test/UnindexedParent"])
async def test_p0_6_identical_real_embedding_blocks_without_structure(
    real_db_session: AsyncSession, parent_iri: str | None,
) -> None:
    """An identical label in pgvector blocks even when a new IRI has no structure."""
    project_id = uuid4()
    project = Project(id=project_id, name="P0-6", owner_id="dedup-owner")
    real_db_session.add(project)
    await real_db_session.flush()
    config = ProjectEmbeddingConfig(
        project_id=project_id,
        provider="local",
        model_name="integration-vector",
        dimensions=3,
    )
    embedding = EntityEmbedding(
        project_id=project_id,
        branch="main",
        entity_iri="https://example.test/LegalEntity",
        entity_type="class",
        label="Legal Entity",
        embedding_text="Legal Entity",
        embedding=[1.0, 0.0, 0.0],
        provider="local",
        model_name="integration-vector",
    )
    real_db_session.add_all([config, embedding])
    await real_db_session.commit()

    service = DuplicateCheckService(real_db_session)
    provider = AsyncMock()
    provider.provider_name = "local"
    provider.model_id = "integration-vector"
    provider.embed_text.return_value = [1.0, 0.0, 0.0]
    service._embedding_svc._get_provider = AsyncMock(return_value=provider)  # type: ignore[method-assign]

    try:
        response = await service.check(project_id, "Legal Entity", parent_iri=parent_iri)
        assert response.verdict == "block"
        assert response.composite_score == pytest.approx(1.0)
    finally:
        await _delete_project(real_db_session, project_id)


@pytest.mark.asyncio
async def test_p0_5_public_project_embedding_spend_requires_membership(
    real_db_session: AsyncSession,
) -> None:
    """A public project does not expose its owner's paid embedding key to strangers."""
    project_id = uuid4()
    project = Project(
        id=project_id,
        name="P0-5",
        owner_id="embedding-owner",
        is_public=True,
    )
    real_db_session.add(project)
    await real_db_session.commit()

    try:
        with pytest.raises(HTTPException) as exc_info:
            await semantic_search._verify_access(
                project_id,
                real_db_session,
                CurrentUser(id="authenticated-stranger", name="Stranger"),
            )
        assert exc_info.value.status_code == 403
    finally:
        await _delete_project(real_db_session, project_id)


@pytest.mark.asyncio
async def test_p1_1_unpriced_model_stops_before_provider_on_real_budget_rows(
    real_db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real project budget cannot be bypassed by an unknown $0 model."""
    project_id = uuid4()
    user = CurrentUser(id="budget-user", name="Budget User")
    project = Project(id=project_id, name="P1-1", owner_id=user.id)
    project.members.append(ProjectMember(user_id=user.id, role="editor"))
    config = ProjectLLMConfig(
        project_id=project_id,
        provider="openai",
        model="unknown-paid-model",
        monthly_budget_usd=1.0,
    )
    real_db_session.add_all([project, config])
    await real_db_session.commit()

    monkeypatch.setattr(pricing, "_pricing_cache", {"known": (0.1, 0.2)})
    monkeypatch.setattr(pricing, "_pricing_fetched_at", time.time())
    provider_factory = AsyncMock()
    monkeypatch.setattr(generation, "get_provider", provider_factory)

    try:
        with pytest.raises(HTTPException) as exc_info:
            await generation.generate_suggestions(
                project_id,
                GenerateSuggestionsRequest(
                    class_iri="https://example.test/Thing",
                    suggestion_type="children",
                ),
                real_db_session,
                user,
            )
        assert exc_info.value.status_code == 503
        provider_factory.assert_not_called()
    finally:
        await _delete_project(real_db_session, project_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "content", "expected_status"),
    [
        ("editor", "not valid turtle", 422),
        (
            "suggester",
            "@prefix ex: <https://example.test/> .\n"
            "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
            "ex:Minted a owl:Class .\n",
            403,
        ),
    ],
)
async def test_p1_7_p1_12_server_gates_content_before_real_git_commit(
    real_db_session: AsyncSession,
    tmp_path: Path,
    role: str,
    content: str,
    expected_status: int,
) -> None:
    """Malformed Turtle and client-hidden minting never reach the branch."""
    project_id = uuid4()
    user = CurrentUser(id=f"write-{role}", name="Writer")
    project = Project(id=project_id, name=f"write-{role}", owner_id="owner")
    project.members.append(ProjectMember(user_id=user.id, role=role))
    session = SuggestionSession(
        project_id=project_id,
        user_id=user.id,
        session_id=f"write-{role}",
        branch=f"suggestion/write-{role}",
        beacon_token="integration-token",
    )
    real_db_session.add_all([project, session])
    await real_db_session.commit()

    git = BareGitRepositoryService(base_path=str(tmp_path))
    initial = b"@prefix ex: <https://example.test/> .\n"
    git.initialize_repository(project_id, initial, "ontology.ttl")
    git.create_branch(project_id, session.branch, from_ref="main")

    try:
        with pytest.raises(HTTPException) as exc_info:
            await SuggestionService(real_db_session, git).save(
                project_id,
                session.session_id,
                SuggestionSaveRequest(
                    content=content,
                    entity_iri="https://example.test/Minted",
                    entity_label="Minted",
                    mints_entity=False,
                ),
                user,
            )
        assert exc_info.value.status_code == expected_status
        assert git.get_file_from_branch(project_id, session.branch, "ontology.ttl") == initial
    finally:
        await _delete_project(real_db_session, project_id)


@pytest.mark.asyncio
async def test_p0_4_failed_merge_keeps_real_session_and_trust_ledger_unchanged(
    real_db_session: AsyncSession,
) -> None:
    """A downstream merge failure cannot mint ACCEPTED trust credit."""
    project_id = uuid4()
    user = CurrentUser(id="review-editor", name="Reviewer")
    project = Project(id=project_id, name="P0-4", owner_id="owner")
    project.members.append(ProjectMember(user_id=user.id, role="editor"))
    session = SuggestionSession(
        project_id=project_id,
        user_id="contributor",
        session_id="p0-4-merge",
        branch="suggestion/p0-4-merge",
        beacon_token="integration-token",
        status=SuggestionSessionStatus.SUBMITTED.value,
        pr_number=42,
    )
    real_db_session.add_all([project, session])
    await real_db_session.commit()

    pull_requests = MagicMock()
    pull_requests.merge_pull_request = AsyncMock(
        side_effect=HTTPException(status_code=409, detail="conflict")
    )
    try:
        with patch(
            "ontokit.services.suggestion_service.get_pull_request_service",
            return_value=pull_requests,
        ), pytest.raises(HTTPException) as exc_info:
            await SuggestionService(real_db_session).approve(
                project_id, session.session_id, user
            )
        assert exc_info.value.status_code == 409
        await real_db_session.refresh(session)
        assert session.status == SuggestionSessionStatus.SUBMITTED.value
        outcome_count = (
            await real_db_session.execute(
                select(func.count())
                .select_from(SuggestionOutcome)
                .where(SuggestionOutcome.session_id == session.id)
            )
        ).scalar_one()
        assert outcome_count == 0
    finally:
        await _delete_project(real_db_session, project_id)
