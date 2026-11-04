"""Full-project embedding jobs: timeout, cancellation, and stale-job recovery.

Hosted UAT: arq killed ``run_embedding_generation_task`` at its 300 s default.
The timeout reaches the job as ``asyncio.CancelledError`` (a BaseException), so
the ``except Exception`` cleanup never ran and the job row stayed ``running``
forever, which made ``GET /embeddings/status`` misreport progress and blocked
new generation through the one-active-job index.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from arq.worker import Function
from sqlalchemy.dialects import postgresql

from ontokit.core.config import Settings, settings
from ontokit.services.embedding_service import (
    EmbeddingService,
    fail_stale_embedding_jobs,
    stale_embedding_job_cutoff,
)
from ontokit.worker import WorkerSettings, run_embedding_generation_task

PROJECT_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")


def _sql(statement: Any) -> str:
    return str(statement.compile(compile_kwargs={"literal_binds": True}))


def _worker_function(name: str) -> Any:
    for entry in WorkerSettings.functions:
        entry_name = entry.name if isinstance(entry, Function) else entry.__name__
        if entry_name == name:
            return entry
    raise AssertionError(f"{name} is not registered")


# ── 1. Per-function arq timeout ──────────────────────────────────────────────


def test_embedding_job_timeout_default_is_two_hours() -> None:
    assert Settings().embedding_job_timeout_seconds == 7200


def test_embedding_job_timeout_must_be_at_least_a_minute() -> None:
    with pytest.raises(ValueError):
        Settings(embedding_job_timeout_seconds=59)


def test_embedding_generation_uses_its_own_arq_timeout() -> None:
    entry = _worker_function("run_embedding_generation_task")
    assert isinstance(entry, Function)
    assert entry.timeout_s == settings.embedding_job_timeout_seconds


def test_embedding_generation_is_never_retried_by_arq() -> None:
    """A retry after shutdown would re-run a job the handlers already failed."""
    entry = _worker_function("run_embedding_generation_task")
    assert entry.max_tries == 1


@pytest.mark.parametrize(
    "name", ["run_single_entity_embed_task", "run_batch_entity_embed_task", "run_lint_task"]
)
def test_other_jobs_keep_the_worker_default_timeout(name: str) -> None:
    entry = _worker_function(name)
    assert not isinstance(entry, Function) or entry.timeout_s is None


# ── 2a. Service: cancellation marks the job failed ───────────────────────────


@pytest.fixture(autouse=True)
def _isolate_branch_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    @asynccontextmanager
    async def unlocked(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
        yield

    monkeypatch.setattr("ontokit.services.embedding_service.branch_write_lock", unlocked)


def _db_cancelled_during_project_load() -> AsyncMock:
    db = AsyncMock()
    db.add = Mock()
    job_result = MagicMock()
    job_result.scalar_one_or_none.return_value = MagicMock(id=uuid.uuid4(), status="pending")
    db.execute.side_effect = [
        job_result,  # select EmbeddingJob
        asyncio.CancelledError(),  # arq timeout lands mid-query
        MagicMock(),  # discard staging rows
        MagicMock(),  # raw UPDATE -> failed
    ]
    return db


async def _embed(db: AsyncMock) -> None:
    with (
        patch("ontokit.services.ontology.get_ontology_service", return_value=MagicMock()),
        patch("ontokit.git.bare_repository.BareGitRepositoryService", return_value=MagicMock()),
        patch("ontokit.services.storage.get_storage_service", return_value=MagicMock()),
    ):
        await EmbeddingService(db).embed_project(PROJECT_ID, "main", uuid.uuid4())


async def test_cancelled_embedding_marks_job_failed_and_reraises() -> None:
    db = _db_cancelled_during_project_load()

    with pytest.raises(asyncio.CancelledError):
        await _embed(db)

    db.rollback.assert_awaited_once()
    update_sql = _sql(db.execute.await_args_list[3].args[0])
    assert "UPDATE embedding_jobs" in update_sql
    assert "'failed'" in update_sql
    assert "EMBEDDING_JOB_TIMEOUT_SECONDS" in update_sql
    db.commit.assert_awaited()


async def test_cleanup_failure_does_not_mask_cancellation() -> None:
    db = _db_cancelled_during_project_load()
    db.rollback.side_effect = RuntimeError("connection is closed")

    with pytest.raises(asyncio.CancelledError):
        await _embed(db)


# ── 2b. Worker backstop: a fresh session marks the job failed ────────────────


def _ctx_with_backstop() -> tuple[dict[str, Any], AsyncMock]:
    backstop = AsyncMock()
    factory = MagicMock(return_value=backstop)
    return {"db": AsyncMock(), "redis": AsyncMock(), "session_factory": factory}, backstop


@pytest.mark.parametrize(
    "error", [asyncio.CancelledError(), TimeoutError(), RuntimeError("boom")], ids=type
)
async def test_worker_backstop_marks_job_failed(error: BaseException) -> None:
    ctx, backstop = _ctx_with_backstop()
    job_id = str(uuid.uuid4())

    with patch("ontokit.services.embedding_service.EmbeddingService") as service_cls:
        service_cls.return_value.embed_project = AsyncMock(side_effect=error)
        with pytest.raises(type(error)):
            await run_embedding_generation_task(ctx, str(PROJECT_ID), "main", job_id)

    update_sql = _sql(backstop.execute.await_args.args[0])
    assert "UPDATE embedding_jobs" in update_sql
    assert "'failed'" in update_sql
    # Only a still-active row is touched; a job the service already finalized keeps its message.
    assert "embedding_jobs.status IN ('pending', 'running')" in update_sql
    assert job_id.replace("-", "") in update_sql.replace("-", "")
    backstop.commit.assert_awaited_once()
    backstop.close.assert_awaited_once()


async def test_worker_backstop_failure_does_not_mask_original_error() -> None:
    ctx, backstop = _ctx_with_backstop()
    backstop.execute.side_effect = RuntimeError("database down")

    with patch("ontokit.services.embedding_service.EmbeddingService") as service_cls:
        service_cls.return_value.embed_project = AsyncMock(side_effect=asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await run_embedding_generation_task(ctx, str(PROJECT_ID), "main", str(uuid.uuid4()))


async def test_worker_success_skips_backstop() -> None:
    ctx, backstop = _ctx_with_backstop()

    with patch("ontokit.services.embedding_service.EmbeddingService") as service_cls:
        service_cls.return_value.embed_project = AsyncMock()
        result = await run_embedding_generation_task(
            ctx, str(PROJECT_ID), "main", str(uuid.uuid4())
        )

    assert result["status"] == "completed"
    backstop.execute.assert_not_awaited()


# ── 2c. Stale active jobs ────────────────────────────────────────────────────


def test_stale_cutoff_is_past_the_arq_timeout() -> None:
    now = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
    cutoff = stale_embedding_job_cutoff(now)
    assert now - cutoff > timedelta(seconds=settings.embedding_job_timeout_seconds)


async def test_fail_stale_jobs_updates_only_old_active_rows() -> None:
    db = AsyncMock()
    db.execute.return_value = MagicMock(rowcount=1)

    assert await fail_stale_embedding_jobs(db, PROJECT_ID) == 1

    sql = _sql(db.execute.await_args.args[0])
    assert "UPDATE embedding_jobs" in sql
    assert "'failed'" in sql
    assert "embedding_jobs.status IN ('pending', 'running')" in sql
    assert "embedding_jobs.started_at <" in sql
    db.commit.assert_awaited_once()


async def test_fail_stale_jobs_without_matches_does_not_commit() -> None:
    db = AsyncMock()
    db.execute.return_value = MagicMock(rowcount=0)

    assert await fail_stale_embedding_jobs(db, PROJECT_ID) == 0
    db.commit.assert_not_awaited()


async def test_status_ignores_a_stale_active_job() -> None:
    db = AsyncMock()
    config_result = MagicMock()
    config_result.scalar_one_or_none.return_value = None
    count_result = MagicMock()
    count_result.scalar.return_value = 0
    job_result = MagicMock()
    job_result.scalar_one_or_none.return_value = None
    last_total = MagicMock()
    last_total.scalar.return_value = None
    db.execute.side_effect = [config_result, count_result, job_result, last_total]

    status = await EmbeddingService(db).get_status(PROJECT_ID, "main")

    assert status.job_in_progress is False
    active_job_sql = _sql(db.execute.await_args_list[2].args[0])
    assert "embedding_jobs.started_at >=" in active_job_sql


async def test_generate_fails_stale_jobs_before_the_active_check() -> None:
    from ontokit.api.routes import embeddings as routes

    calls: list[str] = []
    db = AsyncMock()
    db.add = Mock()
    no_active = MagicMock()
    no_active.scalar_one_or_none.return_value = None

    async def execute(*_args: object, **_kwargs: object) -> MagicMock:
        calls.append("active-check")
        return no_active

    db.execute.side_effect = execute

    async def fail_stale(_db: object, project_id: uuid.UUID) -> int:
        assert project_id == PROJECT_ID
        calls.append("fail-stale")
        return 1

    pool = AsyncMock()
    with (
        patch.object(routes, "_verify_write_access", new=AsyncMock()),
        patch.object(routes, "get_git_service") as git,
        patch.object(routes, "get_arq_pool", new=AsyncMock(return_value=pool)),
        patch.object(routes, "fail_stale_embedding_jobs", new=fail_stale),
    ):
        git.return_value.get_default_branch.return_value = "main"
        await routes.generate_embeddings(PROJECT_ID, db=db, user=MagicMock(), branch=None)

    assert calls[:2] == ["fail-stale", "active-check"]
    pool.enqueue_job.assert_awaited_once()


# ── 3. Execution start and never resurrecting a finished job ─────────────────


def _job_lookup(job: object, *then: object) -> AsyncMock:
    db = AsyncMock()
    db.add = Mock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = job
    db.execute.side_effect = [result, *then]
    return db


@pytest.mark.parametrize("final_status", ["failed", "completed"])
async def test_embed_project_never_resurrects_a_finished_job(final_status: str) -> None:
    job = MagicMock(id=uuid.uuid4(), status=final_status, started_at=None)
    db = _job_lookup(job)

    ran = await EmbeddingService(db).embed_project(PROJECT_ID, "main", job.id)

    assert ran is False
    assert job.status == final_status
    assert job.started_at is None
    assert db.execute.await_count == 1
    db.commit.assert_not_awaited()
    # The lookup locks the row so a concurrent stale-failure cannot interleave.
    lookup = db.execute.await_args_list[0].args[0]
    assert "FOR UPDATE" in str(lookup.compile(dialect=postgresql.dialect()))


async def test_running_flip_restamps_started_at_with_execution_start() -> None:
    """arq's timeout counts from execution, not from the pending-row insert."""
    queued_at = datetime.now(UTC) - timedelta(hours=5)
    job = MagicMock(id=uuid.uuid4(), status="pending", started_at=queued_at)
    db = _job_lookup(job, RuntimeError("stop after the flip"))
    before = datetime.now(UTC)

    with (
        patch("ontokit.services.ontology.get_ontology_service", return_value=MagicMock()),
        patch("ontokit.git.bare_repository.BareGitRepositoryService", return_value=MagicMock()),
        patch("ontokit.services.storage.get_storage_service", return_value=MagicMock()),
        pytest.raises(RuntimeError),
    ):
        await EmbeddingService(db).embed_project(PROJECT_ID, "main", job.id)

    assert job.started_at >= before
    # The flip was committed before the work began.
    db.commit.assert_awaited()


async def test_new_job_row_records_execution_start() -> None:
    db = _job_lookup(None, RuntimeError("stop after the flip"))
    before = datetime.now(UTC)

    with (
        patch("ontokit.services.ontology.get_ontology_service", return_value=MagicMock()),
        patch("ontokit.git.bare_repository.BareGitRepositoryService", return_value=MagicMock()),
        patch("ontokit.services.storage.get_storage_service", return_value=MagicMock()),
        pytest.raises(RuntimeError),
    ):
        await EmbeddingService(db).embed_project(PROJECT_ID, "main", uuid.uuid4())

    added = db.add.call_args.args[0]
    assert added.status == "running"
    assert added.started_at >= before


async def test_worker_reports_a_skipped_job() -> None:
    ctx, backstop = _ctx_with_backstop()

    with patch("ontokit.services.embedding_service.EmbeddingService") as service_cls:
        service_cls.return_value.embed_project = AsyncMock(return_value=False)
        result = await run_embedding_generation_task(
            ctx, str(PROJECT_ID), "main", str(uuid.uuid4())
        )

    assert result["status"] == "skipped"
    backstop.execute.assert_not_awaited()
