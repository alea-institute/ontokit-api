"""Real PostgreSQL/Redis proof that retention preserves retired-demo resolution.

Run against an isolated, migrated PostgreSQL database with pgvector and Redis.
Git repositories are real temporary bare repositories; only the MinIO client is
replaced, so all six production deletion steps (including source protection) run.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ontokit.git.bare_repository import BareGitRepositoryService
from ontokit.models.demo_generation import DemoGeneration
from ontokit.models.embedding import EmbeddingJob, EntityEmbedding, EntityEmbeddingStaging
from ontokit.models.lint import LintIssue, LintRun
from ontokit.models.normalization import NormalizationRun
from ontokit.models.ontology_index import (
    IndexedAnnotation,
    IndexedEntity,
    IndexedHierarchy,
    IndexedLabel,
    IndexingStatus,
    OntologyIndexStatus,
)
from ontokit.models.project import Project, ProjectMember
from ontokit.models.pull_request import GitHubIntegration
from ontokit.services import demo_retention
from ontokit.services.demo_project_provisioning import (
    DEMO_DEFAULT_BRANCH,
    ProvisionedDemo,
    build_demo_generation_key,
    demo_generation_attempt_lease,
    ensure_demo_projects,
    finalize_demo_publication,
    record_demo_preparation,
    resolve_current_demo_project,
)
from ontokit.services.demo_retention import DemoRetentionService, PurgeReceipt, RetentionSummary
from ontokit.services.storage import StorageService

# One row per table per project; index children have their own FK to the entity.
_CONTENT_MODELS = (
    OntologyIndexStatus,
    IndexedEntity,
    IndexedLabel,
    IndexedAnnotation,
    IndexedHierarchy,
    EntityEmbedding,
    EmbeddingJob,
    EntityEmbeddingStaging,
    LintRun,
    LintIssue,
    NormalizationRun,
)
_EXPECTED_COUNTS = {
    "repositories": 1,
    "index": 5,
    "embeddings": 3,
    "lint": 2,
    "normalization": 1,
    "storage": 1,
}


class _ObjectStore:
    """Small in-memory MinIO client; production code chooses what to delete."""

    def __init__(self, bucket: str) -> None:
        self.bucket = bucket
        self.objects: dict[str, bytes] = {}

    def list_objects(
        self, bucket: str, *, prefix: str, recursive: bool
    ) -> Iterator[SimpleNamespace]:
        assert bucket == self.bucket and recursive
        return iter(
            SimpleNamespace(object_name=key)
            for key in tuple(self.objects)
            if key.startswith(prefix)
        )

    def remove_object(self, bucket: str, key: str) -> None:
        assert bucket == self.bucket
        del self.objects[key]


def _commits() -> dict[str, str]:
    """Unique stand-ins for the two pinned upstream repository revisions."""
    return {
        destination: uuid4().hex + uuid4().hex[:8]
        for destination in (
            "alea-institute/ontokit-demo-folio",
            "alea-institute/ontokit-demo-semantic-canon",
        )
    }


async def _prepare(
    db: AsyncSession,
    provisioned: Sequence[ProvisionedDemo],
    git: BareGitRepositoryService,
    objects: _ObjectStore,
) -> None:
    for item in provisioned:
        project_id = item.project_id
        content = (f"<urn:demo:{project_id}> a <http://www.w3.org/2002/07/owl#Class> .\n").encode()
        commit = git.get_repository(project_id).write_file(
            branch_name=DEMO_DEFAULT_BRANCH,
            filepath="ontology.ttl",
            content=content,
            message="Prepare integration demo",
            author_name="Demo integration test",
            author_email="demo-test@example.com",
        )
        objects.objects[f"projects/{project_id}/derived.ttl"] = content
        entity = IndexedEntity(
            project_id=project_id,
            branch=DEMO_DEFAULT_BRANCH,
            iri=f"urn:demo:{project_id}",
            local_name="Demo",
            entity_type="class",
        )
        job = EmbeddingJob(project_id=project_id, branch=DEMO_DEFAULT_BRANCH, status="completed")
        lint = LintRun(project_id=project_id, status="completed", issues_found=1)
        db.add_all([entity, job, lint])
        await db.flush()
        embedding_fields = {
            "project_id": project_id,
            "branch": DEMO_DEFAULT_BRANCH,
            "entity_iri": entity.iri,
            "entity_type": "class",
            "embedding_text": "Demo",
            "embedding": [1.0, 0.0, 0.0],
            "dimensions": 3,
            "provider": "local",
            "model_name": "integration-fixture",
        }
        db.add_all(
            [
                OntologyIndexStatus(
                    project_id=project_id,
                    branch=DEMO_DEFAULT_BRANCH,
                    status=IndexingStatus.READY.value,
                    commit_hash=commit.hash,
                    entity_count=1,
                ),
                IndexedLabel(entity_id=entity.id, property_iri="urn:label", value="Demo"),
                IndexedAnnotation(entity_id=entity.id, property_iri="urn:note", value="Fixture"),
                IndexedHierarchy(
                    project_id=project_id,
                    branch=DEMO_DEFAULT_BRANCH,
                    child_iri=entity.iri,
                    parent_iri="http://www.w3.org/2002/07/owl#Thing",
                ),
                EntityEmbedding(**embedding_fields),
                EntityEmbeddingStaging(job_id=job.id, **embedding_fields),
                LintIssue(
                    project_id=project_id,
                    run_id=lint.id,
                    issue_type="info",
                    rule_id="integration-fixture",
                    message="Fixture lint issue",
                ),
                NormalizationRun(
                    project_id=project_id,
                    report_json="{}",
                    original_format="turtle",
                    original_size_bytes=len(content),
                    normalized_size_bytes=len(content),
                    triple_count=1,
                    commit_hash=commit.hash,
                ),
            ]
        )
        await db.commit()
        await record_demo_preparation(db, item, commit.hash)


async def _rows(
    db: AsyncSession, project_id: UUID, *, identities: bool = False
) -> dict[str, list[str]]:
    """Read persisted column values, including vectors, without ORM identity caching."""
    snapshots = {}
    models = (Project, ProjectMember, GitHubIntegration) if identities else _CONTENT_MODELS
    for model in models:
        table = model.__table__
        if model in (IndexedLabel, IndexedAnnotation):
            condition = table.c.entity_id.in_(
                select(IndexedEntity.id).where(IndexedEntity.project_id == project_id)
            )
        elif model is Project:
            condition = table.c.id == project_id
        else:
            condition = table.c.project_id == project_id
        rows = await db.execute(select(table).where(condition))
        # repr normalizes pgvector's numpy arrays for scalar equality comparisons.
        snapshots[table.name] = sorted(repr(dict(row)) for row in rows.mappings())
    return snapshots


def _repository_files(git: BareGitRepositoryService, project_id: UUID) -> dict[str, bytes]:
    path = git.base_path / f"{project_id}.git"
    assert path.is_dir()
    return {
        str(file.relative_to(path)): file.read_bytes() for file in path.rglob("*") if file.is_file()
    }


async def _public_demo_ids(db: AsyncSession) -> set[UUID]:
    result = await db.execute(
        select(Project.id).where(Project.is_demo.is_(True), Project.is_public.is_(True))
    )
    return set(result.scalars())


def _assert_no_work(summary: RetentionSummary) -> None:
    assert summary.purged == []
    assert summary.failed == []
    assert summary.yielded == []
    assert summary.budget_deferred == []


async def test_retired_redirect_survives_generation_content_retention(
    real_db_session: AsyncSession,
    real_redis: Redis,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = real_db_session
    # Redis is a required host integration prerequisite, even though retention's
    # cross-host lease itself is PostgreSQL-backed.
    assert await real_redis.ping()
    assert isinstance(db.bind, AsyncEngine)
    engine = db.bind

    @asynccontextmanager
    async def lease() -> AsyncIterator[None]:
        async with engine.connect() as connection, demo_generation_attempt_lease(connection):
            yield

    git = BareGitRepositoryService(base_path=str(tmp_path / "repositories"))
    storage = StorageService()
    objects = _ObjectStore(storage.bucket)
    monkeypatch.setattr(storage, "client", objects)
    monkeypatch.setattr(demo_retention, "get_bare_git_service", lambda: git)
    monkeypatch.setattr(demo_retention, "get_storage_service", lambda: storage)
    service = DemoRetentionService(db, min_age_days=0, lease_factory=lease)
    assert service.keep_retired == 1

    # Publication and retention are global operations: refuse a populated demo
    # database before creating anything rather than mutate unrelated generations.
    assert (await db.execute(select(DemoGeneration.id))).first() is None
    sources = [
        Project(id=uuid4(), name=name, owner_id="demo-retention-test-owner", is_public=True)
        for name in ("FOLIO", "Semantic Canon")
    ]
    source_ids = [source.id for source in sources]
    generation_keys: list[str] = []
    generations: list[tuple[ProvisionedDemo, ...]] = []
    try:
        for source, owner, repo, path in (
            (sources[0], "alea-institute", "FOLIO", "FOLIO.owl"),
            (sources[1], "CatholicOS", "ontology-semantic-canon", "ontology.ttl"),
        ):
            source.source_file_path = f"projects/{source.id}/{path}"
            objects.objects[source.source_file_path] = b"shared source ontology"
            db.add(source)
            await db.flush()
            db.add(
                GitHubIntegration(
                    project_id=source.id,
                    repo_owner=owner,
                    repo_name=repo,
                    default_branch="main",
                    ontology_file_path=path,
                    turtle_file_path=path,
                    sync_enabled=True,
                )
            )
        await db.commit()

        for number in range(3):
            key = build_demo_generation_key(_commits())
            generation_keys.append(key)
            provisioned = await ensure_demo_projects(db, key)
            assert len(provisioned) == 2
            generations.append(provisioned)
            await _prepare(db, provisioned, git, objects)
            if number == 2:
                # Before publication, even fully populated preparing content
                # must survive; the previous generation remains active.
                before_rows = {
                    item.project_id: await _rows(db, item.project_id) for item in provisioned
                }
                before_git = {
                    item.project_id: _repository_files(git, item.project_id) for item in provisioned
                }
                before_objects = dict(objects.objects)
                summary = await service.apply()
                _assert_no_work(summary)
                assert {entry.generation_key: entry.reason for entry in summary.retained} == {
                    generation_keys[0]: "keep",
                    generation_keys[1]: "active",
                    key: "preparing",
                }
                for item in provisioned:
                    assert await _rows(db, item.project_id) == before_rows[item.project_id]
                    assert _repository_files(git, item.project_id) == before_git[item.project_id]
                assert objects.objects == before_objects
            await finalize_demo_publication(db, provisioned)
            assert await _public_demo_ids(db) == {item.project_id for item in provisioned}

        oldest, middle, active = generations
        all_items = [item for generation in generations for item in generation]
        oldest_ids = {item.project_id for item in oldest}
        active_by_source = {item.source_repository: item.project_id for item in active}

        # Protect referenced objects *inside* a purge prefix as well as the
        # inherited shared source paths. Exercise raw and bucket-qualified paths,
        # with references belonging to other projects, not the deletion target.
        for source, item, qualify in zip(sources, oldest, (False, True), strict=True):
            protected_key = f"projects/{item.project_id}/referenced-source.ttl"
            objects.objects[protected_key] = b"must survive the purge"
            source.source_file_path = (
                f"{storage.bucket}/{protected_key}" if qualify else protected_key
            )
        await db.commit()

        before_rows = {item.project_id: await _rows(db, item.project_id) for item in all_items}
        before_git = {
            item.project_id: _repository_files(git, item.project_id) for item in all_items
        }
        before_identities = {
            project_id: await _rows(db, project_id, identities=True)
            for project_id in [*source_ids, *(item.project_id for item in all_items)]
        }
        before_objects = dict(objects.objects)
        for rows in before_rows.values():
            assert len(rows) == len(_CONTENT_MODELS)
            assert all(len(values) == 1 for values in rows.values())

        async def assert_resolution() -> None:
            for item in oldest:
                project = await db.get(Project, item.project_id, populate_existing=True)
                assert project is not None and project.is_demo and not project.is_public
                replacement = await resolve_current_demo_project(db, project)
                assert replacement is not None
                assert replacement.id == active_by_source[item.source_repository]
                assert replacement.demo_source_project_id == project.demo_source_project_id

        await assert_resolution()
        # Ensure the zero-day age floor is crossed without sleeping or editing
        # lifecycle timestamps; production eligibility and ordering still run.
        now = datetime.now(UTC) + timedelta(seconds=1)
        service.clock = lambda: now
        plan = await service.plan()
        assert [entry.generation_key for entry in plan.eligible] == [generation_keys[0]]
        assert {entry.generation_key: entry.reason for entry in plan.retained} == {
            generation_keys[1]: "keep",
            generation_keys[2]: "active",
        }
        summary = await service.apply()
        assert summary.purged == [generation_keys[0]]
        assert summary.retained == plan.retained
        assert summary.failed == summary.yielded == summary.budget_deferred == []

        for item in oldest:
            assert not (git.base_path / f"{item.project_id}.git").exists()
            assert all(not rows for rows in (await _rows(db, item.project_id)).values())
        for item in (*middle, *active):
            assert await _rows(db, item.project_id) == before_rows[item.project_id]
            assert _repository_files(git, item.project_id) == before_git[item.project_id]
        expected_objects = {
            key: value
            for key, value in before_objects.items()
            if key not in {f"projects/{project_id}/derived.ttl" for project_id in oldest_ids}
        }
        assert objects.objects == expected_objects
        for project_id, rows in before_identities.items():
            assert await _rows(db, project_id, identities=True) == rows
        await assert_resolution()
        assert await _public_demo_ids(db) == {item.project_id for item in active}

        generation = await db.get(DemoGeneration, oldest[0].generation_id, populate_existing=True)
        assert generation is not None and generation.status == "retired"
        assert generation.purged_at == now
        assert generation.purge_receipt is not None
        receipt_text = generation.purge_receipt
        receipt = PurgeReceipt.model_validate_json(receipt_text)
        assert receipt.generation_id == generation.id
        assert receipt.generation_key == generation_keys[0]
        assert set(receipt.project_ids) == oldest_ids
        integration_ids = await db.execute(
            select(GitHubIntegration.id).where(GitHubIntegration.project_id.in_(oldest_ids))
        )
        assert set(receipt.integration_ids) == set(integration_ids.scalars())
        assert set(receipt.retained) == {
            "demo_generations",
            "projects",
            "github_integrations",
            "source_file_path_objects",
        }
        assert receipt.retained_counts == {
            "demo_generations": 1,
            "projects": 2,
            "github_integrations": 2,
        }
        assert len(receipt.attempts) == 1
        attempt = receipt.attempts[0]
        assert attempt.outcome == "success" and attempt.failure_class is None
        assert attempt.started_at == attempt.finished_at == now
        assert attempt.completed_steps == list(_EXPECTED_COUNTS)
        assert attempt.counts == {step: count * 2 for step, count in _EXPECTED_COUNTS.items()}
        assert len(attempt.results) == 2 * len(_EXPECTED_COUNTS)
        assert {(result.project_id, result.step): result.count for result in attempt.results} == {
            (project_id, step): count
            for project_id in oldest_ids
            for step, count in _EXPECTED_COUNTS.items()
        }
        for items, status in ((middle, "retired"), (active, "active")):
            retained = await db.get(DemoGeneration, items[0].generation_id, populate_existing=True)
            assert retained is not None and retained.status == status
            assert retained.purged_at is None and retained.purge_receipt is None

        second = await service.apply()
        _assert_no_work(second)
        assert {entry.generation_key: entry.reason for entry in second.retained} == {
            generation_keys[0]: "purged",
            generation_keys[1]: "keep",
            generation_keys[2]: "active",
        }
        await db.refresh(generation)
        assert generation.purge_receipt == receipt_text and generation.purged_at == now
        assert objects.objects == expected_objects
        for item in (*middle, *active):
            assert await _rows(db, item.project_id) == before_rows[item.project_id]
            assert _repository_files(git, item.project_id) == before_git[item.project_id]
        await assert_resolution()
    finally:
        # Provisioning and retention commit internally, so rollback alone cannot
        # clean up. Remove only this test's rows, in FK-safe order.
        await db.rollback()
        await db.execute(delete(Project).where(Project.demo_source_project_id.in_(source_ids)))
        await db.execute(
            delete(DemoGeneration).where(DemoGeneration.generation_key.in_(generation_keys))
        )
        await db.execute(delete(Project).where(Project.id.in_(source_ids)))
        await db.commit()
