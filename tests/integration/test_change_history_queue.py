"""Real-Redis proof that every landing is reconciled by the ARQ worker."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from arq import ArqRedis, create_pool, func
from arq.connections import RedisSettings
from arq.worker import Worker
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ontokit import worker
from ontokit.core.config import settings
from ontokit.git.bare_repository import BareGitRepositoryService
from ontokit.models.change_event import EntityChangeEvent
from ontokit.models.change_history_watermark import ChangeHistoryWatermark
from ontokit.models.project import Project
from ontokit.services.change_history_reconciler import enqueue_change_history
from tests.integration.change_history_process import commit

pytestmark = pytest.mark.integration

BRANCH = "main"


@pytest_asyncio.fixture
async def queue() -> AsyncIterator[ArqRedis]:
    """An empty job queue in a Redis database no other test uses."""
    if not os.environ.get("REDIS_URL"):
        pytest.skip("REDIS_URL not set")
    redis_settings = RedisSettings.from_dsn(os.environ["REDIS_URL"])
    redis_settings.database = 7
    pool = await create_pool(redis_settings)
    await pool.flushdb()
    try:
        yield pool
    finally:
        await pool.flushdb()
        await pool.aclose()


@pytest_asyncio.fixture
async def project(real_db_session: AsyncSession) -> AsyncIterator[UUID]:
    project_id = uuid4()
    real_db_session.add(Project(id=project_id, name="Queue test ontology", owner_id="owner"))
    await real_db_session.commit()
    try:
        yield project_id
    finally:
        await real_db_session.rollback()
        await real_db_session.execute(delete(Project).where(Project.id == project_id))
        await real_db_session.commit()


@pytest.mark.asyncio
async def test_a_landing_during_a_running_job_still_gets_reconciled(
    real_db_session: AsyncSession,
    project: UUID,
    queue: ArqRedis,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "git_repos_base_path", str(tmp_path))
    git = BareGitRepositoryService()
    first = commit(git, project, BRANCH, 1)
    await enqueue_change_history(project, BRANCH, pool=queue)
    # ARQ holds a job's id from enqueue until the job finishes. A second landing
    # in that time must queue its own job instead of being treated as a duplicate.
    second = commit(git, project, BRANCH, 2)
    await enqueue_change_history(project, BRANCH, pool=queue)
    await enqueue_change_history(project, BRANCH, pool=queue)

    assert sorted(job.job_id or "" for job in await queue.queued_jobs()) == sorted(
        f"change-history:{project}:{BRANCH}:{head}" for head in (first, second)
    )

    jobs = Worker(
        functions=[func(worker.reconcile_change_history_task, keep_result=0, max_tries=1000)],
        redis_pool=queue,
        burst=True,
        poll_delay=0.05,
        handle_signals=False,
        ctx={"session_factory": async_sessionmaker(real_db_session.bind, expire_on_commit=False)},
    )
    await jobs.main()

    assert (jobs.jobs_complete, jobs.jobs_failed) == (2, 0)
    events = (
        await real_db_session.scalars(
            select(EntityChangeEvent).where(EntityChangeEvent.project_id == project)
        )
    ).all()
    assert sorted((event.commit_hash, event.entity_iri.rsplit("#", 1)[1]) for event in events) == (
        sorted([(str(first), "Class0"), (str(second), "Class1")])
    )
    watermark = await real_db_session.get(ChangeHistoryWatermark, (project, BRANCH))
    assert watermark is not None and watermark.commit_hash == str(second)
    assert await queue.queued_jobs() == []
