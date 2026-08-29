"""Concurrency proof for whole-document source revision compare-and-set saves."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from rdflib import Graph
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.api.routes.projects import get_file_at_revision, save_source_content
from ontokit.core.auth import CurrentUser
from ontokit.git import BareGitRepositoryService, BareOntologyRepository
from ontokit.main import app
from ontokit.schemas.project import SourceContentSave, SourceContentSaveResponse
from ontokit.services.normalization_service import NormalizationService
from ontokit.services.storage import StorageError

BASE = """\
@prefix ex: <https://example.test/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
ex:Ontology a owl:Ontology .
"""


class RecordingStorage:
    """Record uploads and yield so concurrent requests can overlap before locking."""

    def __init__(self, initial: bytes = BASE.encode()) -> None:
        self.uploads: list[bytes] = []
        self.current = initial

    async def upload_file(self, _path: str, content: bytes, _content_type: str) -> str:
        self.uploads.append(content)
        self.current = content
        await asyncio.sleep(0)
        return "source/ontology.ttl"


class FailingStorage:
    """Fail after CAS so the route must release its transaction lock cleanly."""

    def __init__(self) -> None:
        self.attempts = 0

    async def upload_file(self, _path: str, _content: bytes, _content_type: str) -> str:
        self.attempts += 1
        raise StorageError("storage unavailable")


class FailingCompensationStorage(RecordingStorage):
    """Accept the source write, then fail while restoring the old mirror."""

    async def upload_file(self, path: str, content: bytes, content_type: str) -> str:
        if self.uploads:
            raise StorageError("compensation unavailable")
        return await super().upload_file(path, content, content_type)


class InterleavingStorage(RecordingStorage):
    """Pause the source mirror update while it still owns the branch lock."""

    def __init__(self) -> None:
        super().__init__()
        self.source_upload_started = asyncio.Event()
        self.release_source_upload = asyncio.Event()
        self.downloads = 0

    async def upload_file(self, path: str, content: bytes, content_type: str) -> str:
        if b"ex:SourceWriter" in content:
            self.source_upload_started.set()
            await self.release_source_upload.wait()
        return await super().upload_file(path, content, content_type)

    async def download_file(self, _path: str) -> bytes:
        self.downloads += 1
        return self.current


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


@pytest.mark.parametrize(
    "base_revision",
    [
        "a" * 39,
        "a" * 41,
        "g" * 40,
        f"{'a' * 40} ",
        "HEAD",
        "",
    ],
)
def test_save_schema_rejects_noncanonical_base_revisions(base_revision: str) -> None:
    """Only exact full hexadecimal commit identities enter the CAS path."""
    with pytest.raises(ValidationError):
        SourceContentSave(content=BASE, commit_message="Invalid base", base_revision=base_revision)


def test_save_schema_normalizes_uppercase_base_revision() -> None:
    """Equivalent uppercase Git object identities compare canonically."""
    data = SourceContentSave(
        content=BASE,
        commit_message="Uppercase base",
        base_revision="A" * 40,
    )

    assert data.base_revision == "a" * 40


def test_openapi_documents_typed_source_revision_conflict() -> None:
    """Generated clients can discover the stable stale-write error envelope."""
    operation = app.openapi()["paths"]["/api/v1/projects/{project_id}/source"]["put"]
    conflict = operation["responses"]["409"]

    assert conflict["description"] == "The supplied base revision is stale."
    assert conflict["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/SourceRevisionConflictResponse"
    }
    detail_schema = app.openapi()["components"]["schemas"]["SourceRevisionConflictDetail"]
    assert detail_schema["properties"]["code"]["const"] == "SOURCE_REVISION_CONFLICT"
    assert set(detail_schema["required"]) == {
        "message",
        "base_revision",
        "current_revision",
        "branch",
    }


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
async def test_revision_file_content_stays_paired_when_branch_advances(tmp_path: Path) -> None:
    """A branch move between resolution and read cannot produce a torn response."""
    project_id = uuid4()
    git = BareGitRepositoryService(base_path=str(tmp_path))
    initial = git.initialize_repository(project_id, BASE.encode(), "ontology.ttl")
    repository = git.get_repository(project_id)
    immutable_read = repository.get_file_at_version
    advanced = False

    def advance_then_read(filename: str, revision: str) -> str:
        nonlocal advanced
        if not advanced:
            advanced = True
            git.commit_changes(
                project_id=project_id,
                ontology_content=(BASE + "ex:Later a owl:Class .\n").encode(),
                filename="ontology.ttl",
                message="Advance branch during read",
                author_name="Concurrent Writer",
                author_email="writer@example.test",
                branch_name="main",
            )
        return immutable_read(filename, revision)

    service = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(git_ontology_path="ontology.ttl"))
    )
    with patch.object(repository, "get_file_at_version", side_effect=advance_then_read):
        result = await get_file_at_revision(
            project_id,
            service,
            MagicMock(
                repository_exists=MagicMock(return_value=True),
                get_repository=MagicMock(return_value=repository),
            ),
            None,
            version="main",
            filename="ontology.ttl",
        )

    assert result.revision == initial.hash
    assert result.content == BASE
    assert repository.get_branch_commit_hash("main") != result.revision


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
async def test_late_expected_head_mismatch_returns_typed_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A writer that advances after the route pre-check still wins without side effects."""
    project_id = uuid4()
    git = BareGitRepositoryService(base_path=str(tmp_path))
    initial = git.initialize_repository(project_id, BASE.encode(), "ontology.ttl")
    alternate_content = (BASE + "ex:Alternate a owl:Class .\n").encode()
    compare_and_swap = BareOntologyRepository._compare_and_swap_branch_head
    advanced = False

    def advance_before_compare_and_swap(
        repository: BareOntologyRepository,
        branch_name: str,
        *,
        new_head: str,
        expected_head: str,
    ) -> None:
        nonlocal advanced
        if not advanced:
            advanced = True
            git.commit_changes(
                project_id,
                alternate_content,
                "ontology.ttl",
                "Alternate writer",
                branch_name="main",
            )
        compare_and_swap(
            repository,
            branch_name,
            new_head=new_head,
            expected_head=expected_head,
        )

    monkeypatch.setattr(
        BareOntologyRepository,
        "_compare_and_swap_branch_head",
        advance_before_compare_and_swap,
    )
    storage = RecordingStorage()
    record_events = AsyncMock(return_value=[])
    get_arq_pool = AsyncMock(return_value=None)

    with (
        patch("ontokit.api.routes.projects.get_arq_pool", get_arq_pool),
        pytest.raises(HTTPException) as caught,
    ):
        await save_source_content(
            project_id,
            SourceContentSave(
                content=BASE + "ex:Guarded a owl:Class .\n",
                commit_message="Guarded writer",
                base_revision=initial.hash,
            ),
            _db_session(),
            SimpleNamespace(
                get=AsyncMock(
                    return_value=SimpleNamespace(
                        user_role="owner",
                        source_file_path="ontology.ttl",
                        git_ontology_path=None,
                    )
                )
            ),
            storage,
            GitBackedOntology(),
            git,
            SimpleNamespace(record_events_from_diff=record_events),
            SimpleNamespace(),
            CurrentUser(id="owner", name="Owner", email="owner@example.test"),
            branch="main",
        )

    alternate_head = git.get_repository(project_id).get_branch_commit_hash("main")
    assert caught.value.status_code == 409
    assert caught.value.detail == {
        "code": "SOURCE_REVISION_CONFLICT",
        "message": "The ontology source changed after it was loaded; reload before saving.",
        "base_revision": initial.hash,
        "current_revision": alternate_head,
        "branch": "main",
    }
    assert git.get_file_from_branch(project_id, "main", "ontology.ttl") == alternate_content
    assert storage.uploads == []
    record_events.assert_not_awaited()
    get_arq_pool.assert_not_awaited()


@pytest.mark.asyncio
async def test_database_commit_failure_restores_git_and_storage(tmp_path: Path) -> None:
    """A failed final transaction is compensated under the branch lock."""
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
    db = _db_session()
    db.commit.side_effect = RuntimeError("database unavailable")

    with pytest.raises(HTTPException) as caught:
        await save_source_content(
            project_id,
            SourceContentSave(
                content=BASE + "ex:RolledBack a owl:Class .\n",
                commit_message="Must roll back",
                base_revision=initial.hash,
            ),
            db,
            service,
            storage,
            ontology,
            git,
            SimpleNamespace(record_events_from_diff=AsyncMock(return_value=[])),
            SimpleNamespace(),
            CurrentUser(id="owner", name="Owner", email="owner@example.test"),
            branch="main",
        )

    assert caught.value.status_code == 500
    assert caught.value.detail == (
        "The save transaction could not be finalized; changes were rolled back."
    )
    assert git.get_repository(project_id).get_branch_commit_hash("main") == initial.hash
    assert storage.current == BASE.encode()
    assert len(storage.uploads) == 2
    assert ontology.graphs == {}
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_database_failure_reports_incomplete_git_compensation(tmp_path: Path) -> None:
    """A guarded-ref mismatch is surfaced for operator reconciliation."""
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
    db = _db_session()
    db.commit.side_effect = RuntimeError("database unavailable")

    with (
        patch.object(git, "restore_branch_head", return_value=False),
        pytest.raises(HTTPException) as caught,
    ):
        await save_source_content(
            project_id,
            SourceContentSave(
                content=BASE + "ex:NeedsReconcile a owl:Class .\n",
                commit_message="Fault injection",
                base_revision=initial.hash,
            ),
            db,
            service,
            RecordingStorage(),
            GitBackedOntology(),
            git,
            SimpleNamespace(record_events_from_diff=AsyncMock(return_value=[])),
            SimpleNamespace(),
            CurrentUser(id="owner", name="Owner", email="owner@example.test"),
            branch="main",
        )

    assert caught.value.status_code == 500
    assert caught.value.detail == {
        "code": "SOURCE_SAVE_CONSISTENCY_FAILURE",
        "message": (
            "The save transaction failed and compensation was incomplete; "
            "operator reconciliation is required."
        ),
        "commit_hash": git.get_repository(project_id).get_branch_commit_hash("main"),
        "branch": "main",
        "git_restored": False,
        "storage_restored": False,
    }
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_database_failure_reports_incomplete_storage_compensation(tmp_path: Path) -> None:
    """A failed object restoration is surfaced after Git is safely restored."""
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
    db = _db_session()
    db.commit.side_effect = RuntimeError("database unavailable")
    storage = FailingCompensationStorage()

    with pytest.raises(HTTPException) as caught:
        await save_source_content(
            project_id,
            SourceContentSave(
                content=BASE + "ex:StorageMismatch a owl:Class .\n",
                commit_message="Fault injection",
                base_revision=initial.hash,
            ),
            db,
            service,
            storage,
            GitBackedOntology(),
            git,
            SimpleNamespace(record_events_from_diff=AsyncMock(return_value=[])),
            SimpleNamespace(),
            CurrentUser(id="owner", name="Owner", email="owner@example.test"),
            branch="main",
        )

    assert caught.value.status_code == 500
    assert caught.value.detail["code"] == "SOURCE_SAVE_CONSISTENCY_FAILURE"
    assert caught.value.detail["message"] == (
        "The save transaction failed and compensation was incomplete; "
        "operator reconciliation is required."
    )
    assert len(caught.value.detail["commit_hash"]) == 40
    assert caught.value.detail["commit_hash"] != initial.hash
    assert caught.value.detail["branch"] == "main"
    assert caught.value.detail["git_restored"] is True
    assert caught.value.detail["storage_restored"] is False
    assert git.get_repository(project_id).get_branch_commit_hash("main") == initial.hash
    assert storage.current != BASE.encode()
    db.rollback.assert_awaited_once()


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


@pytest.mark.asyncio
async def test_source_save_serializes_with_normalization_writer(tmp_path: Path) -> None:
    """Normalization cannot read or write the branch mid-source-save."""
    project_id = uuid4()
    git = BareGitRepositoryService(base_path=str(tmp_path))
    initial = git.initialize_repository(project_id, BASE.encode(), "ontology.ttl")
    project = SimpleNamespace(
        id=project_id,
        user_role="owner",
        source_file_path="ontology.ttl",
        git_ontology_path=None,
    )
    storage = InterleavingStorage()
    source_db = _db_session()
    normalization_db = _db_session()
    normalization = NormalizationService(normalization_db, storage, git)
    report = SimpleNamespace(
        triple_count=2,
        prefixes_removed=[],
        prefixes_added=[],
        original_format="turtle",
        original_size_bytes=len(BASE),
        normalized_size_bytes=len(BASE) + 36,
        format_converted=False,
        to_dict=lambda: {},
    )
    normalization.extractor = SimpleNamespace(
        normalize_to_turtle=lambda content, _filename: (
            content + b"ex:NormalizedWriter a owl:Class .\n",
            report,
        )
    )

    async def save_source() -> SourceContentSaveResponse:
        return await save_source_content(
            project_id,
            SourceContentSave(
                content=BASE + "ex:SourceWriter a owl:Class .\n",
                commit_message="Source writer",
                base_revision=initial.hash,
            ),
            source_db,
            SimpleNamespace(get=AsyncMock(return_value=project)),
            storage,
            GitBackedOntology(),
            git,
            SimpleNamespace(record_events_from_diff=AsyncMock(return_value=[])),
            SimpleNamespace(),
            CurrentUser(id="owner", name="Owner", email="owner@example.test"),
            branch="main",
        )

    with (
        patch("ontokit.api.routes.projects.get_arq_pool", AsyncMock(return_value=None)),
        patch(
            "ontokit.services.translation_jobs.enqueue_label_diff_after_commit",
            AsyncMock(),
        ),
    ):
        source_task = asyncio.create_task(save_source())
        await storage.source_upload_started.wait()
        normalization_task = asyncio.create_task(normalization.run_normalization(project))
        await asyncio.sleep(0)

        assert not normalization_task.done()
        assert storage.downloads == 0

        storage.release_source_upload.set()
        source_result, normalization_result = await asyncio.gather(
            source_task,
            normalization_task,
        )

    normalization_run, _, _ = normalization_result
    history = git.get_history(project_id, branch="main", all_branches=False)
    assert history[0].hash == normalization_run.commit_hash
    assert history[0].parent_hashes == [source_result.commit_hash]
    assert history[1].parent_hashes == [initial.hash]
    assert storage.downloads == 1
