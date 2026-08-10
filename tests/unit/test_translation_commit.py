"""Proof-first coverage for gated translation commits (U5)."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pygit2
import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDFS

from ontokit.core.constants import ONTOKIT_COMMITTER_EMAIL
from ontokit.git.bare_repository import BareGitRepositoryService, BareOntologyRepository
from ontokit.models.llm_config import ProjectLLMConfig
from ontokit.models.translation import ProjectTranslationConfig, hash_literal_value
from ontokit.services.branch_lock import branch_write_lock
from ontokit.services.translation_annotations import read_annotation
from ontokit.services.translation_service import TranslationResult, TranslationService

ENTITY = URIRef("http://example.org/ontology#Person")


class FixtureGitService(BareGitRepositoryService):
    """Exercise the real service seam while routing it to the conftest bare repo."""

    def __init__(self, repo: BareOntologyRepository) -> None:
        super().__init__(str(repo.repo_path.parent))
        self._fixture_repo = repo

    def get_repository(self, project_id: UUID) -> BareOntologyRepository:  # noqa: ARG002
        return self._fixture_repo


def _service(
    bare_git_repo: BareOntologyRepository,
    *,
    project_id: UUID,
    enqueue_index: AsyncMock | None = None,
) -> tuple[TranslationService, AsyncMock, FixtureGitService]:
    db = AsyncMock()
    db.add = Mock()
    translation = ProjectTranslationConfig(
        project_id=project_id,
        language_tags=["fr", "es", "de"],
        verification_mechanism="confidence",
        confidence_threshold=0.8,
        primary_provider="openai",
        primary_model="gpt-5.4-mini",
    )
    llm = ProjectLLMConfig(project_id=project_id, provider="openai", model="unused")
    git = FixtureGitService(bare_git_repo)
    service = TranslationService(
        db,
        translation,
        llm,
        "translation-engine",
        git_service=git,
        index_enqueuer=enqueue_index,
    )
    return service, db, git


def _result(language: str, value: str, *, accepted: bool = True) -> TranslationResult:
    return TranslationResult(
        language=language,
        proposed_value=value,
        score=0.95 if accepted else 0.5,
        method="confidence",
        threshold=0.8,
        accepted=accepted,
    )


async def _apply(
    service: TranslationService,
    results: dict[str, TranslationResult],
    *,
    source_value: str = "Person",
) -> Any:
    return await service.apply_results(
        branch="main",
        filename="ontology.ttl",
        entity_iri=str(ENTITY),
        predicate=str(RDFS.label),
        source_value=source_value,
        source_language="en",
        source_value_hash=hash_literal_value(source_value),
        results=results,
        model_version="2026-08-09",
    )


@pytest.mark.asyncio
async def test_verified_translation_commits_literal_annotation_and_split_identity(
    bare_git_repo: BareOntologyRepository,
) -> None:
    project_id = uuid4()
    service, _db, _git = _service(bare_git_repo, project_id=project_id)

    outcome = await _apply(service, {"fr": _result("fr", "Personne")})

    assert outcome.commit is not None
    commit = bare_git_repo.repo[outcome.commit.hash]
    assert isinstance(commit, pygit2.Commit)
    assert commit.author.name == "translation-bot"
    assert commit.author.email == "translation-engine@ontokit.dev"
    assert commit.committer.name == "OntoKit-bot"
    assert commit.committer.email == ONTOKIT_COMMITTER_EMAIL

    graph = Graph().parse(data=bare_git_repo.read_file("main", "ontology.ttl"), format="turtle")
    literal = Literal("Personne", lang="fr")
    assert (ENTITY, RDFS.label, literal) in graph
    annotation = read_annotation(graph, ENTITY, RDFS.label, literal)
    assert annotation is not None
    assert annotation.state == "verified"
    assert annotation.method == "confidence"


@pytest.mark.asyncio
async def test_below_threshold_is_provisional_without_git_commit(
    bare_git_repo: BareOntologyRepository,
) -> None:
    service, db, _git = _service(bare_git_repo, project_id=uuid4())
    before = bare_git_repo.get_branch_commit_hash("main")

    outcome = await _apply(service, {"fr": _result("fr", "Personne", accepted=False)})

    assert outcome.commit is None
    assert bare_git_repo.get_branch_commit_hash("main") == before
    record = db.add.call_args.args[0]
    assert record.state == "provisional"


@pytest.mark.asyncio
async def test_record_flush_failure_cannot_orphan_git_annotation(
    bare_git_repo: BareOntologyRepository,
) -> None:
    service, db, _git = _service(bare_git_repo, project_id=uuid4())
    before = bare_git_repo.get_branch_commit_hash("main")
    db.flush.side_effect = RuntimeError("database flush failed")

    with pytest.raises(RuntimeError, match="database flush failed"):
        await _apply(service, {"fr": _result("fr", "Personne")})

    assert bare_git_repo.get_branch_commit_hash("main") == before
    graph = Graph().parse(data=bare_git_repo.read_file("main", "ontology.ttl"), format="turtle")
    assert (ENTITY, RDFS.label, Literal("Personne", lang="fr")) not in graph


@pytest.mark.asyncio
async def test_user_save_and_translation_commit_are_serialized_without_lost_update(
    bare_git_repo: BareOntologyRepository,
) -> None:
    project_id = uuid4()
    service, _db, git = _service(bare_git_repo, project_id=project_id)
    user_started = asyncio.Event()

    async def user_save() -> None:
        async with branch_write_lock(service._db, project_id, "main"):
            content = git.get_file_from_branch(project_id, "main", "ontology.ttl")
            user_started.set()
            await asyncio.sleep(0)
            git.commit_changes(
                project_id,
                content + b"\n<http://example.org/ontology#Person> "
                b'<http://www.w3.org/2000/01/rdf-schema#comment> "User edit"@en .\n',
                "ontology.ttl",
                "User save",
                branch_name="main",
            )

    user_task = asyncio.create_task(user_save())
    await user_started.wait()
    translation_task = asyncio.create_task(_apply(service, {"fr": _result("fr", "Personne")}))
    await asyncio.gather(user_task, translation_task)

    graph = Graph().parse(data=git.get_file_from_branch(project_id, "main", "ontology.ttl"))
    assert (ENTITY, RDFS.comment, Literal("User edit", lang="en")) in graph
    assert (ENTITY, RDFS.label, Literal("Personne", lang="fr")) in graph


@pytest.mark.asyncio
async def test_stale_source_snapshot_is_discarded_without_translation_commit(
    bare_git_repo: BareOntologyRepository,
) -> None:
    project_id = uuid4()
    service, _db, git = _service(bare_git_repo, project_id=project_id)
    content = git.get_file_from_branch(project_id, "main", "ontology.ttl")
    git.commit_changes(
        project_id,
        content.replace(b'"Person"@en', b'"Human"@en'),
        "ontology.ttl",
        "Edit source label",
        branch_name="main",
    )
    before = bare_git_repo.get_branch_commit_hash("main")

    outcome = await _apply(service, {"fr": _result("fr", "Personne")})

    assert outcome.commit is None
    assert outcome.discarded_languages == ("fr",)
    assert bare_git_repo.get_branch_commit_hash("main") == before


@pytest.mark.asyncio
async def test_verified_language_burst_coalesces_commit_and_index_enqueue(
    bare_git_repo: BareOntologyRepository,
) -> None:
    enqueue_index = AsyncMock()
    service, _db, _git = _service(bare_git_repo, project_id=uuid4(), enqueue_index=enqueue_index)

    outcome = await _apply(
        service,
        {
            "fr": _result("fr", "Personne"),
            "es": _result("es", "Persona"),
            "de": _result("de", "Person"),
        },
    )

    assert outcome.commit is not None
    assert outcome.committed_languages == ("de", "es", "fr")
    assert len(bare_git_repo.get_history(branch="main", all_branches=False)) == 2
    enqueue_index.assert_awaited_once_with(
        project_id=service.project_id, branch="main", commit_hash=outcome.commit.hash
    )


@pytest.mark.asyncio
async def test_translation_diff_adds_only_target_entity_and_axiom_triples(
    bare_git_repo: BareOntologyRepository,
) -> None:
    service, _db, _git = _service(bare_git_repo, project_id=uuid4())
    outcome = await _apply(service, {"fr": _result("fr", "Personne")})
    assert outcome.commit is not None

    commit = bare_git_repo.repo[outcome.commit.hash]
    assert isinstance(commit, pygit2.Commit)
    diff = bare_git_repo.repo.diff(commit.parents[0], commit)
    added = [
        line.content.strip()
        for patch in diff
        for hunk in patch.hunks
        for line in hunk.lines
        if line.origin == "+" and line.content.strip()
    ]
    assert added
    updated = bare_git_repo.read_file(outcome.commit.hash, "ontology.ttl").decode()
    assert "owl:Axiom" in updated
    assert Literal("Personne", lang="fr") in Graph().parse(data=updated, format="turtle").objects(
        ENTITY, RDFS.label
    )
