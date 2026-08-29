"""Concurrency proof for whole-document source revision compare-and-set saves."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from rdflib import Graph
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.api.routes.projects import get_file_at_revision, save_source_content
from ontokit.core.auth import CurrentUser
from ontokit.git import BareGitRepositoryService
from ontokit.schemas.project import SourceContentSave, SourceContentSaveResponse
from ontokit.services.storage import StorageError

BASE = """\
@prefix ex: <https://example.test/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
ex:Ontology a owl:Ontology .
"""


class RecordingStorage:
    """Record uploads and yield so concurrent requests can overlap before locking."""

    def __init__(self) -> None:
        self.uploads: list[bytes] = []

    async def upload_file(self, _path: str, content: bytes, _content_type: str) -> str:
        self.uploads.append(content)
        await asyncio.sleep(0)
        return "source/ontology.ttl"


class FailingStorage:
    """Fail after CAS so the route must release its transaction lock cleanly."""

    def __init__(self) -> None:
        self.attempts = 0

    async def upload_file(self, _path: str, _content: bytes, _content_type: str) -> str:
        self.attempts += 1
        raise StorageError("storage unavailable")


class GitBackedOntology:
    """Exercise real Git reads and RDF parsing while retaining route cache semantics."""

    def __init__(self) -> None:
        self.graphs: dict[tuple[UUID, str], Graph] = {}

    def is_loaded(self, project_id: UUID, branch: str) -> bool:
        return (project_id, branch) in self.graphs

    async def load_from_git(
        self,
        project_id: UUID,
        branch: str,
        filename: str,
        git: BareGitRepositoryService,
    ) -> Graph:
        graph = Graph().parse(
            data=git.get_file_from_branch(project_id, branch, filename),
            format="turtle",
        )
        self.graphs[(project_id, branch)] = graph
        return graph

    async def _get_graph(self, project_id: UUID, branch: str) -> Graph:
        return self.graphs[(project_id, branch)]

    def unload(self, project_id: UUID, branch: str) -> None:
        self.graphs.pop((project_id, branch), None)


def _db_session() -> AsyncMock:
    db = AsyncMock(spec=AsyncSession)
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


def test_save_schema_requires_full_base_revision() -> None:
    """A save cannot be constructed without the immutable revision read contract."""
    with pytest.raises(ValidationError):
        SourceContentSave(content=BASE, commit_message="No base")  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_revision_file_resolves_branch_to_immutable_hash(tmp_path: Path) -> None:
    """A symbolic version is preserved while content is read by its resolved commit."""
    project_id = uuid4()
    git = BareGitRepositoryService(base_path=str(tmp_path))
    initial = git.initialize_repository(project_id, BASE.encode(), "ontology.ttl")
    service = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(git_ontology_path="ontology.ttl"))
    )

    result = await get_file_at_revision(
        project_id,
        service,
        git,
        None,
        version="main",
        filename="ontology.ttl",
    )

    assert result.version == "main"
    assert result.revision == initial.hash
    assert result.content == BASE


@pytest.mark.asyncio
async def test_stale_revision_has_no_write_side_effects(tmp_path: Path) -> None:
    """CAS rejection occurs before storage, Git, ontology, events, and jobs."""
    project_id = uuid4()
    git = BareGitRepositoryService(base_path=str(tmp_path))
    initial = git.initialize_repository(project_id, BASE.encode(), "ontology.ttl")
    service = SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(
                user_role="owner",
                source_file_path="ontology.ttl",
                git_ontology_path=None,
            )
        )
    )
    storage = RecordingStorage()
    ontology = GitBackedOntology()
    record_events = AsyncMock(return_value=[])
    change_service = SimpleNamespace(record_events_from_diff=record_events)
    db = _db_session()
    get_arq_pool = AsyncMock(return_value=None)
    enqueue_label_diff = AsyncMock()

    with (
        patch("ontokit.api.routes.projects.get_arq_pool", get_arq_pool),
        patch(
            "ontokit.services.translation_jobs.enqueue_label_diff_after_commit",
            enqueue_label_diff,
        ),
        pytest.raises(HTTPException) as caught,
    ):
        await save_source_content(
            project_id,
            SourceContentSave(
                content=BASE + "ex:Stale a owl:Class .\n",
                commit_message="Stale",
                base_revision="0" * 40,
            ),
            db,
            service,
            storage,
            ontology,
            git,
            change_service,
            SimpleNamespace(),
            CurrentUser(id="owner", name="Owner", email="owner@example.test"),
            branch="main",
        )

    assert caught.value.status_code == 409
    assert caught.value.detail == {
        "code": "SOURCE_REVISION_CONFLICT",
        "message": "The ontology source changed after it was loaded; reload before saving.",
        "base_revision": "0" * 40,
        "current_revision": initial.hash,
        "branch": "main",
    }
    assert storage.uploads == []
    assert ontology.graphs == {}
    record_events.assert_not_awaited()
    get_arq_pool.assert_not_awaited()
    enqueue_label_diff.assert_not_awaited()
    db.commit.assert_not_awaited()
    db.rollback.assert_awaited_once()
    assert git.get_repository(project_id).get_branch_commit_hash("main") == initial.hash


@pytest.mark.asyncio
async def test_matching_revision_storage_failure_does_not_commit(tmp_path: Path) -> None:
    """A post-CAS storage failure rolls back the advisory transaction and leaves Git intact."""
    project_id = uuid4()
    git = BareGitRepositoryService(base_path=str(tmp_path))
    initial = git.initialize_repository(project_id, BASE.encode(), "ontology.ttl")
    service = SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(
                user_role="owner",
                source_file_path="ontology.ttl",
                git_ontology_path=None,
            )
        )
    )
    storage = FailingStorage()
    ontology = GitBackedOntology()
    record_events = AsyncMock(return_value=[])
    db = _db_session()

    with pytest.raises(HTTPException) as caught:
        await save_source_content(
            project_id,
            SourceContentSave(
                content=BASE + "ex:Update a owl:Class .\n",
                commit_message="Update",
                base_revision=initial.hash,
            ),
            db,
            service,
            storage,
            ontology,
            git,
            SimpleNamespace(record_events_from_diff=record_events),
            SimpleNamespace(),
            CurrentUser(id="owner", name="Owner", email="owner@example.test"),
            branch="main",
        )

    assert caught.value.status_code == 503
    assert storage.attempts == 1
    assert ontology.graphs == {}
    record_events.assert_not_awaited()
    db.commit.assert_not_awaited()
    db.rollback.assert_awaited_once()
    assert git.get_repository(project_id).get_branch_commit_hash("main") == initial.hash


@pytest.mark.asyncio
async def test_two_concurrent_same_base_writes_exactly_one_wins(tmp_path: Path) -> None:
    """The real branch lock and bare Git head make one stale peer lose without retry."""
    project_id = uuid4()
    git = BareGitRepositoryService(base_path=str(tmp_path))
    git.initialize_repository(project_id, BASE.encode(), "ontology.ttl")
    base_revision = git.get_repository(project_id).get_branch_commit_hash("main")
    user = CurrentUser(id="owner", name="Owner", email="owner@example.test")
    project = SimpleNamespace(
        user_role="owner",
        source_file_path="ontology.ttl",
        git_ontology_path=None,
    )
    service = SimpleNamespace(get=AsyncMock(return_value=project))
    storage = RecordingStorage()
    ontology = GitBackedOntology()
    change_service = SimpleNamespace(record_events_from_diff=AsyncMock(return_value=[]))
    embed_service = SimpleNamespace()

    async def save(label: str) -> SourceContentSaveResponse:
        content = BASE + f"ex:{label} a owl:Class .\n"
        return await save_source_content(
            project_id,
            SourceContentSave(
                content=content,
                commit_message=f"Add {label}",
                base_revision=base_revision,
            ),
            _db_session(),
            service,
            storage,
            ontology,
            git,
            change_service,
            embed_service,
            user,
            branch="main",
        )

    with (
        patch("ontokit.api.routes.projects.get_arq_pool", AsyncMock(return_value=None)),
        patch(
            "ontokit.services.translation_jobs.enqueue_label_diff_after_commit",
            AsyncMock(),
        ),
    ):
        outcomes = await asyncio.gather(save("Alpha"), save("Beta"), return_exceptions=True)

    successes = [outcome for outcome in outcomes if isinstance(outcome, SourceContentSaveResponse)]
    conflicts = [outcome for outcome in outcomes if isinstance(outcome, HTTPException)]
    assert len(successes) == 1
    assert len(conflicts) == 1
    assert conflicts[0].status_code == 409
    assert conflicts[0].detail["code"] == "SOURCE_REVISION_CONFLICT"
    assert conflicts[0].detail["base_revision"] == base_revision
    assert conflicts[0].detail["current_revision"] == successes[0].commit_hash
    assert len(storage.uploads) == 1
    assert git.get_repository(project_id).get_branch_commit_hash("main") == successes[0].commit_hash
    assert len(git.get_history(project_id, branch="main", all_branches=False)) == 2
