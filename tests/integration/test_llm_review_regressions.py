"""Live regression proofs for the 2026-08-08 LLM subsystem review."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from rdflib import Graph
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
from ontokit.schemas.suggestion import (
    SuggestionBeaconRequest,
    SuggestionSaveRequest,
    SuggestionSubmitRequest,
    SuggestionSubmitResponse,
)
from ontokit.services.duplicate_check_service import DuplicateCheckService
from ontokit.services.embedding_service import EmbeddingService
from ontokit.services.llm import pricing
from ontokit.services.suggestion_service import (
    MAX_NEW_ENTITIES_PER_SUBMISSION,
    SuggestionService,
)

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
        assert (
            git.get_repository(project_id)
            .read_file(session.branch, "ontology.ttl")
            .endswith(b"ex:Thing a ex:Class .\n")
        )
    finally:
        await _delete_project(real_db_session, project_id)


@pytest.mark.asyncio
async def test_f5_storage_key_path_saves_to_existing_root_ontology(
    real_db_session: AsyncSession, tmp_path: Path
) -> None:
    """A storage object key never becomes a shadow path in the Git tree."""
    project_id = uuid4()
    user = CurrentUser(id="f5-path-user", name="F5 Path User")
    project = Project(
        id=project_id,
        name="F5 path truth",
        owner_id=user.id,
        source_file_path=f"ontokit/projects/{project_id}/ontology.ttl",
    )
    project.members.append(ProjectMember(user_id=user.id, role="editor"))
    session = SuggestionSession(
        project_id=project_id,
        user_id=user.id,
        user_name=user.name,
        session_id="f5-path-save",
        branch="suggestion/f5-path-save",
        beacon_token="integration-token",
    )
    real_db_session.add_all([project, session])
    await real_db_session.commit()

    git = BareGitRepositoryService(base_path=str(tmp_path))
    initial = b"@prefix ex: <https://example.test/> .\n"
    git.initialize_repository(project_id, initial, "ontology.ttl")
    git.create_branch(project_id, session.branch, from_ref="main")
    suggestions = SuggestionService(real_db_session, git)
    suggestions._enqueue_branch_refresh = AsyncMock()  # type: ignore[method-assign]

    try:
        content = initial.decode() + "ex:Thing a ex:Class .\n"
        await suggestions.save(
            project_id,
            session.session_id,
            SuggestionSaveRequest(
                content=content,
                entity_iri="https://example.test/Thing",
                entity_label="Thing",
            ),
            user,
        )

        repo = git.get_repository(project_id)
        assert repo.read_file(session.branch, "ontology.ttl") == content.encode()
        assert repo.list_files(session.branch) == ["ontology.ttl"]
        suggestions._enqueue_branch_refresh.assert_awaited_once_with(
            project_id, session.branch, entity_iri="https://example.test/Thing"
        )
    finally:
        await _delete_project(real_db_session, project_id)


@pytest.mark.asyncio
async def test_f5_missing_resolved_baseline_names_existing_ontology_path(
    real_db_session: AsyncSession, tmp_path: Path
) -> None:
    """A wrong resolved path is an inconsistency, not an empty baseline."""
    project_id = uuid4()
    git = BareGitRepositoryService(base_path=str(tmp_path))
    content = b"@prefix ex: <https://example.test/> .\nex:Existing a ex:Class .\n"
    git.initialize_repository(project_id, content, "ontology.ttl")
    suggestions = SuggestionService(real_db_session, git)
    wrong_path = f"ontokit/projects/{project_id}/ontology.ttl"

    with pytest.raises(HTTPException) as exc_info:
        await suggestions._validate_submission_content(
            project_id, "suggestion/f5-inconsistent", wrong_path, content.decode(), "f5-path-user"
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == {
        "message": "Ontology path is misconfigured for this project",
        "code": "ONTOLOGY_PATH_MISMATCH",
    }


@pytest.mark.asyncio
async def test_f5_submit_rejects_oversized_new_entity_sweep_before_duplicate_checks(
    real_db_session: AsyncSession, tmp_path: Path
) -> None:
    """Submissions over the integrity-check bound fail before semantic ANN work."""
    project_id = uuid4()
    git = BareGitRepositoryService(base_path=str(tmp_path))
    initial = b"""\
@prefix ex: <https://example.test/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
"""
    git.initialize_repository(project_id, initial, "ontology.ttl")
    proposed = initial.decode() + "".join(
        f'ex:New{index} a owl:Class ; rdfs:label "New {index}" .\n'
        for index in range(MAX_NEW_ENTITIES_PER_SUBMISSION + 1)
    )
    suggestions = SuggestionService(real_db_session, git)

    with (
        patch.object(DuplicateCheckService, "check", new=AsyncMock()) as duplicate_check,
        pytest.raises(HTTPException) as exc_info,
    ):
        await suggestions._validate_submission_content(
            project_id, "suggestion/f5-too-large", "ontology.ttl", proposed, "f5-limit-user"
        )

    assert duplicate_check.await_count == 0
    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == {
        "message": "Suggestion adds too many entities for duplicate validation",
        "code": "SUGGESTION_ENTITY_LIMIT",
        "new_entity_count": MAX_NEW_ENTITIES_PER_SUBMISSION + 1,
        "max_new_entities": MAX_NEW_ENTITIES_PER_SUBMISSION,
    }


@pytest.mark.asyncio
async def test_r2_1_saved_entity_embedding_does_not_block_its_own_submit(
    real_db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live pgvector row minted on this suggestion branch is excluded at submit."""
    project_id = uuid4()
    user = CurrentUser(id="r2-self-user", name="Self User", email="self@example.test")
    project = Project(id=project_id, name="R2 self", owner_id=user.id)
    project.members.append(ProjectMember(user_id=user.id, role="editor"))
    session = SuggestionSession(
        project_id=project_id,
        user_id=user.id,
        user_name=user.name,
        session_id="r2-self-submit",
        branch="suggestion/r2-self-submit",
        beacon_token="integration-token",
    )
    real_db_session.add_all(
        [
            project,
            session,
            ProjectEmbeddingConfig(
                project_id=project_id,
                provider="local",
                model_name="integration-vector",
                dimensions=3,
            ),
        ]
    )
    await real_db_session.commit()

    git = BareGitRepositoryService(base_path=str(tmp_path))
    initial = (
        b"@prefix ex: <https://example.test/> .\n"
        b"@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        b"@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
    )
    git.initialize_repository(project_id, initial, "ontology.ttl")
    git.create_branch(project_id, session.branch, from_ref="main")
    minted_iri = f"http://example.org/ontology/{project_id}#Minted"
    content = (
        initial.decode() + f'<{minted_iri}> a owl:Class ; rdfs:label "Minted concept" ; '
        "rdfs:subClassOf owl:Thing .\n"
    )
    suggestions = SuggestionService(real_db_session, git)
    suggestions._enqueue_branch_refresh = AsyncMock()  # type: ignore[method-assign]

    try:
        await suggestions.save(
            project_id,
            session.session_id,
            SuggestionSaveRequest(
                content=content,
                entity_iri=minted_iri,
                entity_label="Minted concept",
            ),
            user,
        )

        graph = Graph().parse(data=content, format="turtle")
        ontology = MagicMock()
        ontology.is_loaded.return_value = True
        ontology._get_graph = AsyncMock(return_value=graph)
        monkeypatch.setattr("ontokit.services.ontology.get_ontology_service", lambda: ontology)
        embedder = EmbeddingService(real_db_session)
        provider = AsyncMock()
        provider.provider_name = "local"
        provider.model_id = "integration-vector"
        provider.dimensions = 3
        provider.embed_text.return_value = [1.0, 0.0, 0.0]
        monkeypatch.setattr(EmbeddingService, "_get_provider", AsyncMock(return_value=provider))
        await embedder.embed_single_entity(project_id, session.branch, minted_iri)

        claim = AsyncMock(
            return_value=SuggestionSubmitResponse(pr_number=1, pr_url=None, status="submitted")
        )
        suggestions._create_pr_for_session_already_locked = claim  # type: ignore[method-assign]
        result = await suggestions.submit(
            project_id,
            session.session_id,
            SuggestionSubmitRequest(summary="ready"),
            user,
        )
        assert result.status == "submitted"
        assert suggestions._enqueue_branch_refresh.await_args_list == [
            call(project_id, session.branch, entity_iri=minted_iri),
            call(project_id, session.branch, full_embedding=True),
        ]
    finally:
        await _delete_project(real_db_session, project_id)


@pytest.mark.asyncio
async def test_r3_restriction_parent_baseline_allows_mint_save_and_submit(
    real_db_session: AsyncSession, tmp_path: Path
) -> None:
    """Reparsed restriction blank nodes do not become malformed mint parents."""
    project_id = uuid4()
    user = CurrentUser(id="r3-restriction-user", name="R3 User", email="r3@example.test")
    project = Project(
        id=project_id,
        name="R3 restrictions",
        owner_id=user.id,
        ontology_iri="https://project.example/ontology",
    )
    project.members.append(ProjectMember(user_id=user.id, role="editor"))
    session = SuggestionSession(
        project_id=project_id,
        user_id=user.id,
        user_name=user.name,
        session_id="r3-restriction-submit",
        branch="suggestion/r3-restriction-submit",
        beacon_token="integration-token",
    )
    real_db_session.add_all([project, session])
    await real_db_session.commit()

    git = BareGitRepositoryService(base_path=str(tmp_path))
    initial = b"""\
@prefix ex: <http://example.org/ontology/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

ex:RestrictedWork a owl:Class ;
    rdfs:label "Restricted work" ;
    rdfs:subClassOf [
        a owl:Restriction ;
        owl:onProperty ex:hasRisk ;
        owl:someValuesFrom ex:Risk
    ] .
"""
    git.initialize_repository(project_id, initial, "ontology.ttl")
    git.create_branch(project_id, session.branch, from_ref="main")
    minted_iri = "https://folio.example/ontology/Minted"
    content = (
        initial.decode() + f'\n<{minted_iri}> a owl:Class ; rdfs:label "Minted" ; '
        "rdfs:subClassOf ex:RestrictedWork, [\n"
        "    a owl:Restriction ;\n"
        "    owl:onProperty ex:hasRisk ;\n"
        "    owl:someValuesFrom ex:Risk\n"
        "] .\n"
    )
    suggestions = SuggestionService(real_db_session, git)
    suggestions._enqueue_branch_refresh = AsyncMock()  # type: ignore[method-assign]
    suggestions._create_pr_for_session_already_locked = AsyncMock(  # type: ignore[method-assign]
        return_value=SuggestionSubmitResponse(pr_number=1, pr_url=None, status="submitted")
    )

    try:
        await suggestions.save(
            project_id,
            session.session_id,
            SuggestionSaveRequest(
                content=content,
                entity_iri=minted_iri,
                entity_label="Minted",
            ),
            user,
        )
        result = await suggestions.submit(
            project_id,
            session.session_id,
            SuggestionSubmitRequest(summary="ready"),
            user,
        )

        assert result.status == "submitted"
        assert suggestions._enqueue_branch_refresh.await_args_list == [
            call(project_id, session.branch, entity_iri=minted_iri),
            call(project_id, session.branch, full_embedding=True),
        ]
    finally:
        await _delete_project(real_db_session, project_id)


@pytest.mark.asyncio
async def test_f3_folio_parent_mint_submits_outside_project_namespace(
    real_db_session: AsyncSession, tmp_path: Path
) -> None:
    """A well-formed FOLIO mint and parent are not restricted to a derived namespace."""
    project_id = uuid4()
    user = CurrentUser(id="f3-folio-user", name="F3 User")
    project = Project(
        id=project_id,
        name="F3 FOLIO mint",
        owner_id=user.id,
        ontology_iri="https://project.example/ontology",
    )
    project.members.append(ProjectMember(user_id=user.id, role="editor"))
    session = SuggestionSession(
        project_id=project_id,
        user_id=user.id,
        session_id="f3-folio-submit",
        branch="suggestion/f3-folio-submit",
        beacon_token="integration-token",
    )
    real_db_session.add_all([project, session])
    await real_db_session.commit()

    git = BareGitRepositoryService(base_path=str(tmp_path))
    initial = b"""\
@prefix folio: <https://folio.example/ontology/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

folio:Actor a owl:Class ; rdfs:label "Actor" .
"""
    git.initialize_repository(project_id, initial, "ontology.ttl")
    git.create_branch(project_id, session.branch, from_ref="main")
    minted_iri = "https://folio.example/ontology/ZorpticWidgetClaim"
    content = (
        initial.decode() + f'\n<{minted_iri}> a owl:Class ; rdfs:label "Zorptic Widget Claim"@en ; '
        "rdfs:subClassOf folio:Actor .\n"
    )
    suggestions = SuggestionService(real_db_session, git)
    suggestions._enqueue_branch_refresh = AsyncMock()  # type: ignore[method-assign]
    suggestions._create_pr_for_session_already_locked = AsyncMock(  # type: ignore[method-assign]
        return_value=SuggestionSubmitResponse(pr_number=1, pr_url=None, status="submitted")
    )

    try:
        await suggestions.save(
            project_id,
            session.session_id,
            SuggestionSaveRequest(
                content=content,
                entity_iri=minted_iri,
                entity_label="Zorptic Widget Claim",
            ),
            user,
        )
        result = await suggestions.submit(
            project_id,
            session.session_id,
            SuggestionSubmitRequest(summary="ready"),
            user,
        )
        assert result.status == "submitted"
        assert suggestions._enqueue_branch_refresh.await_args_list == [
            call(project_id, session.branch, entity_iri=minted_iri),
            call(project_id, session.branch, full_embedding=True),
        ]
    finally:
        await _delete_project(real_db_session, project_id)


@pytest.mark.asyncio
async def test_f3_existing_label_still_returns_duplicate_409(
    real_db_session: AsyncSession, tmp_path: Path
) -> None:
    """Exact baseline label duplication remains a 409 before mint validation."""
    project_id = uuid4()
    user = CurrentUser(id="f3-duplicate-user", name="F3 Duplicate User")
    project = Project(id=project_id, name="F3 duplicate", owner_id=user.id)
    project.members.append(ProjectMember(user_id=user.id, role="editor"))
    session = SuggestionSession(
        project_id=project_id,
        user_id=user.id,
        session_id="f3-duplicate-submit",
        branch="suggestion/f3-duplicate-submit",
        beacon_token="integration-token",
    )
    real_db_session.add_all([project, session])
    await real_db_session.commit()

    git = BareGitRepositoryService(base_path=str(tmp_path))
    initial = b"""\
@prefix ex: <https://folio.example/ontology/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

ex:Actor a owl:Class ; rdfs:label "Actor" .
ex:Existing a owl:Class ; rdfs:label "Zorptic Widget Claim"@en .
"""
    git.initialize_repository(project_id, initial, "ontology.ttl")
    git.create_branch(project_id, session.branch, from_ref="main")
    content = (
        initial.decode() + '\nex:Minted a owl:Class ; rdfs:label "Zorptic Widget Claim"@en ; '
        "rdfs:subClassOf ex:Actor .\n"
    )
    suggestions = SuggestionService(real_db_session, git)
    suggestions._enqueue_branch_refresh = AsyncMock()  # type: ignore[method-assign]

    try:
        await suggestions.save(
            project_id,
            session.session_id,
            SuggestionSaveRequest(
                content=content,
                entity_iri="https://folio.example/ontology/Minted",
                entity_label="Zorptic Widget Claim",
            ),
            user,
        )
        with pytest.raises(HTTPException) as exc_info:
            await suggestions.submit(
                project_id,
                session.session_id,
                SuggestionSubmitRequest(summary="ready"),
                user,
            )
        assert exc_info.value.status_code == 409
        assert exc_info.value.detail == "Suggestion duplicates an existing entity label"
        suggestions._enqueue_branch_refresh.assert_awaited_once_with(
            project_id, session.branch, entity_iri="https://folio.example/ontology/Minted"
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
    real_db_session: AsyncSession,
    parent_iri: str | None,
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
        dimensions=3,
        provider="local",
        model_name="integration-vector",
    )
    real_db_session.add_all([config, embedding])
    await real_db_session.commit()

    service = DuplicateCheckService(real_db_session)
    provider = AsyncMock()
    provider.provider_name = "local"
    provider.model_id = "integration-vector"
    provider.dimensions = 3
    provider.embed_text.return_value = [1.0, 0.0, 0.0]
    provider_factory = AsyncMock(return_value=provider)
    service._embedding_svc._get_provider = provider_factory  # type: ignore[method-assign]

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
    ("entry_point", "hint"),
    [
        ("save", None),
        ("save", False),
        ("save_anonymous", None),
        ("save_anonymous", False),
        ("beacon_save", None),
        ("beacon_save_anonymous", None),
    ],
)
@pytest.mark.parametrize(
    ("declaration", "expected_status"),
    [
        pytest.param("not valid turtle", 422, id="malformed"),
        pytest.param("ex:Minted a owl:Class .", 403, id="class"),
        *[
            pytest.param(content, 403, id=f"{schema_type}-{case}")
            for schema_type in (
                "owl:DeprecatedClass",
                "owl:DeprecatedProperty",
                "rdfs:ContainerMembershipProperty",
            )
            for case, content in [
                ("new-schema", f"ex:Minted a {schema_type} ."),
                ("mixed-denial", f'ex:Existing rdfs:label "edited" . ex:Minted a {schema_type} .'),
            ]
        ],
        pytest.param("ex:Minted a owl:NamedIndividual .", 403, id="explicit-individual"),
        pytest.param("ex:Minted a ex:ExternalClass .", 403, id="ordinary-individual"),
        pytest.param(
            "ex:Minted a [ a owl:Restriction ; owl:onProperty ex:p ; "
            "owl:someValuesFrom ex:ExternalClass ] .",
            403,
            id="class-expression-instance",
        ),
    ],
)
async def test_p1_7_p1_12_server_gates_content_before_real_git_commit(
    real_db_session: AsyncSession,
    tmp_path: Path,
    entry_point: str,
    hint: bool | None,
    declaration: str,
    expected_status: int,
) -> None:
    """Every public writer refuses hidden minting before Git or session mutation."""
    project_id = uuid4()
    anonymous = entry_point.endswith("anonymous")
    user = CurrentUser(id="write-suggester", name="Writer")
    project = Project(id=project_id, name="write-gate", owner_id="owner", is_public=True)
    project.members.append(
        ProjectMember(user_id=user.id, role="editor" if expected_status == 422 else "suggester")
    )
    session = SuggestionSession(
        project_id=project_id,
        user_id=user.id,
        session_id="write-gate",
        branch="suggestion/write-gate",
        beacon_token="integration-token",
        is_anonymous=anonymous,
    )
    real_db_session.add_all([project, session])
    await real_db_session.commit()

    try:
        git = BareGitRepositoryService(base_path=str(tmp_path))
        initial = (
            b"@prefix ex: <https://example.test/> .\n"
            b"@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
            b"@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
            b'ex:Existing a owl:Class ; rdfs:label "original" .\n'
        )
        git.initialize_repository(project_id, initial, "ontology.ttl")
        git.create_branch(project_id, session.branch, from_ref="main")
        head = git.get_repository(project_id).get_branch_commit_hash(session.branch)
        fields = (
            "changes_count",
            "anonymous_content_bytes",
            "entities_modified",
            "last_activity",
            "revision",
            "status",
            "summary",
        )
        before = {field: getattr(session, field) for field in fields}
        content = initial.decode() + declaration
        if declaration.startswith('ex:Existing rdfs:label "edited" . '):
            content = initial.decode().replace('rdfs:label "original"', 'rdfs:label "edited"')
            content += declaration.removeprefix('ex:Existing rdfs:label "edited" . ')

        service = SuggestionService(real_db_session, git)

        with (
            patch.object(service, "_enqueue_branch_refresh", new_callable=AsyncMock) as refresh,
            patch(
                "ontokit.services.suggestion_service.verify_beacon_token",
                return_value=session.session_id,
            ),
            pytest.raises(HTTPException) as exc_info,
        ):
            if entry_point.startswith("beacon"):
                beacon = SuggestionBeaconRequest(session_id=session.session_id, content=content)
                await getattr(service, entry_point)(project_id, beacon, session.session_id)
            else:
                request = SuggestionSaveRequest(
                    content=content,
                    entity_iri="https://example.test/Minted",
                    entity_label="Minted",
                    **({} if hint is None else {"mints_entity": hint}),
                )
                await getattr(service, entry_point)(
                    project_id,
                    session.session_id,
                    request,
                    session.session_id if anonymous else user,
                )
        assert exc_info.value.status_code == expected_status
        if expected_status == 403:
            assert exc_info.value.detail["reason"] == "trust_required_to_mint"
        assert git.get_file_from_branch(project_id, session.branch, "ontology.ttl") == initial
        assert git.get_repository(project_id).get_branch_commit_hash(session.branch) == head
        assert {field: getattr(session, field) for field in fields} == before
        await real_db_session.refresh(session)
        assert {field: getattr(session, field) for field in fields} == before
        refresh.assert_not_awaited()
    finally:
        await _delete_project(real_db_session, project_id)
    assert await real_db_session.scalar(select(Project.id).where(Project.id == project_id)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entry_point", ["save", "save_anonymous", "beacon_save", "beacon_save_anonymous"]
)
@pytest.mark.parametrize(
    ("schema_type", "replacement"),
    [
        ("owl:DeprecatedClass", "ex:Ordinary"),
        ("owl:DeprecatedProperty", "owl:DeprecatedProperty"),
        ("rdfs:ContainerMembershipProperty", "rdfs:ContainerMembershipProperty"),
    ],
)
async def test_schema_identity_edit_persists_after_trust_loss(
    real_db_session: AsyncSession,
    tmp_path: Path,
    entry_point: str,
    schema_type: str,
    replacement: str,
) -> None:
    """Only the suggestion branch owns the identity; revoked trust still permits edits."""
    project_id = uuid4()
    user = CurrentUser(id="schema-editor", name="Schema Editor")
    anonymous = entry_point.endswith("anonymous")
    project = Project(id=project_id, name="schema-edit", owner_id="owner", is_public=True)
    member = ProjectMember(user_id=user.id, role="suggester", is_trusted=True)
    project.members.append(member)
    session = SuggestionSession(
        project_id=project_id,
        user_id=user.id,
        user_name=user.name,
        session_id="schema-edit",
        branch="suggestion/schema-edit",
        beacon_token="integration-token",
        is_anonymous=anonymous,
        changes_count=2,
        revision=3,
        entities_modified=json.dumps(["Before"]),
        anonymous_content_bytes=0,
    )
    real_db_session.add_all([project, session])
    await real_db_session.commit()
    try:
        git = BareGitRepositoryService(base_path=str(tmp_path))
        initial = (
            "@prefix ex: <https://example.test/> .\n"
            "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
            "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
        )
        git.initialize_repository(project_id, initial.encode(), "ontology.ttl")
        git.create_branch(project_id, session.branch, from_ref="main")
        baseline = initial + f'ex:Existing a {schema_type} ; rdfs:label "before" .\n'
        git.commit_changes(
            project_id=project_id,
            branch_name=session.branch,
            ontology_content=baseline.encode(),
            filename="ontology.ttl",
            message="Seed existing branch identity",
            author_name="Fixture",
            author_email="fixture@example.test",
        )
        head = git.get_repository(project_id).get_branch_commit_hash(session.branch)
        if not anonymous:
            member.is_trusted = False
            member.trust_override = "revoked"
            await real_db_session.commit()
            await real_db_session.refresh(member)
            await real_db_session.refresh(project, attribute_names=["members"])
            assert project.members[0].is_trusted is False
            assert project.members[0].trust_override == "revoked"
        before_activity = session.last_activity
        content = initial + f'ex:Existing a {replacement} ; rdfs:label "edited" .\n'
        service = SuggestionService(real_db_session, git)
        with (
            patch.object(service, "_enqueue_branch_refresh", new_callable=AsyncMock) as refresh,
            patch(
                "ontokit.services.suggestion_service.verify_beacon_token",
                return_value=session.session_id,
            ),
        ):
            if entry_point.startswith("beacon"):
                await getattr(service, entry_point)(
                    project_id,
                    SuggestionBeaconRequest(session_id=session.session_id, content=content),
                    session.session_id,
                )
            else:
                await getattr(service, entry_point)(
                    project_id,
                    session.session_id,
                    SuggestionSaveRequest(
                        content=content,
                        entity_iri="https://example.test/Existing",
                        entity_label="Edited",
                        mints_entity=False,
                    ),
                    session.session_id if anonymous else user,
                )
            if entry_point == "save":
                refresh.assert_awaited_once_with(
                    project_id, session.branch, entity_iri="https://example.test/Existing"
                )
            else:
                refresh.assert_not_awaited()
        assert git.get_file_from_branch(project_id, "main", "ontology.ttl") == initial.encode()
        assert (
            git.get_file_from_branch(project_id, session.branch, "ontology.ttl") == content.encode()
        )
        assert git.get_repository(project_id).get_branch_commit_hash(session.branch) != head
        await real_db_session.refresh(session)
        assert session.changes_count == 3
        assert session.revision == 3
        assert session.last_activity > before_activity
        assert session.status == SuggestionSessionStatus.ACTIVE.value
        assert json.loads(session.entities_modified) == (
            ["Before"] if entry_point.startswith("beacon") else ["Before", "Edited"]
        )
        assert session.anonymous_content_bytes == (len(content.encode()) if anonymous else 0)
    finally:
        await _delete_project(real_db_session, project_id)
    assert await real_db_session.scalar(select(Project.id).where(Project.id == project_id)) is None


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
        with (
            patch(
                "ontokit.services.suggestion_service.get_pull_request_service",
                return_value=pull_requests,
            ),
            pytest.raises(HTTPException) as exc_info,
        ):
            await SuggestionService(real_db_session).approve(project_id, session.session_id, user)
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
