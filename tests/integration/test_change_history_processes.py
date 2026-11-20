"""Real-PostgreSQL proof that change-history work is safe across processes."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.git.bare_repository import BareGitRepositoryService
from ontokit.models.change_event import EntityChangeEvent
from ontokit.models.change_history_watermark import ChangeHistoryWatermark
from ontokit.models.project import Project
from tests.integration.change_history_process import commit

pytestmark = pytest.mark.integration

ROOT = Path(__file__).parents[2]
BRANCH = "main"
COMMITS = 60


@pytest_asyncio.fixture
async def project(real_db_session: AsyncSession) -> AsyncIterator[UUID]:
    project_id = uuid4()
    real_db_session.add(Project(id=project_id, name="Process test ontology", owner_id="owner"))
    await real_db_session.commit()
    try:
        yield project_id
    finally:
        await real_db_session.rollback()
        await real_db_session.execute(delete(Project).where(Project.id == project_id))
        await real_db_session.commit()


def _start(mode: str, repos: Path, project: UUID, *arguments: object) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tests.integration.change_history_process",
            mode,
            str(repos),
            str(project),
            BRANCH,
            *map(str, arguments),
        ],
        cwd=ROOT,
        env=os.environ.copy(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


async def _line(process: subprocess.Popen[str]) -> dict[str, object]:
    assert process.stdout is not None
    line = await asyncio.wait_for(asyncio.to_thread(process.stdout.readline), timeout=60)
    assert line, process.stderr.read() if process.stderr else ""
    return json.loads(line)


async def _finish(process: subprocess.Popen[str]) -> None:
    try:
        _, errors = await asyncio.to_thread(process.communicate, timeout=120)
    finally:
        if process.poll() is None:
            process.kill()
            await asyncio.to_thread(process.communicate)
    assert process.returncode == 0, errors


async def _events(db: AsyncSession, project: UUID) -> list[EntityChangeEvent]:
    await db.rollback()
    rows = await db.scalars(
        select(EntityChangeEvent)
        .where(EntityChangeEvent.project_id == project)
        .execution_options(populate_existing=True)
    )
    return list(rows)


@pytest.mark.asyncio
async def test_two_worker_processes_record_each_commit_once(
    real_db_session: AsyncSession, project: UUID, tmp_path: Path
) -> None:
    git = BareGitRepositoryService(str(tmp_path))
    for classes in range(1, COMMITS + 1):
        head = commit(git, project, BRANCH, classes)

    start_at = time.time() + 3  # Both interpreters finish importing before the race.
    workers = [_start("reconcile", tmp_path, project, start_at, 4) for _ in range(2)]
    results = [await _line(worker) for worker in workers]
    for worker in workers:
        await _finish(worker)

    events = await _events(real_db_session, project)
    assert sum(result["recorded"] for result in results) == COMMITS
    assert sum(result["processed"] for result in results) == COMMITS
    assert len(events) == COMMITS
    assert len({(event.commit_hash, event.entity_iri) for event in events}) == COMMITS
    assert {event.event_type for event in events} == {"create"}
    watermark = await real_db_session.get(
        ChangeHistoryWatermark, (project, BRANCH), populate_existing=True
    )
    assert watermark is not None and watermark.commit_hash == str(head)
    # Feed order follows landing order even though two processes wrote the rows.
    ordered = sorted(events, key=lambda event: event.created_at)
    assert [event.entity_iri.rsplit("Class", 1)[1] for event in ordered] == [
        str(number) for number in range(COMMITS)
    ]
    print(f"\ntwo-process reconciliation: {results}")


@pytest.mark.asyncio
async def test_reconciler_waits_for_a_merge_in_another_process_and_credits_its_author(
    real_db_session: AsyncSession, project: UUID, tmp_path: Path
) -> None:
    git = BareGitRepositoryService(str(tmp_path))
    commit(git, project, BRANCH, 1)
    reconciler = _start("once", tmp_path, project)
    assert (await _line(reconciler))["recorded"] == 1
    await _finish(reconciler)
    git.get_repository(project).create_branch("proposal", BRANCH)
    commit(git, project, "proposal", 2)

    merge = _start("merge", tmp_path, project)
    merged = (await _line(merge))["merge_commit"]

    # The target ref has moved, but the merge transaction has not committed.
    reconciler = _start("once", tmp_path, project)
    blocked = await _line(reconciler)
    await _finish(reconciler)
    assert (blocked["processed"], blocked["recorded"], blocked["more"]) == (0, 0, True)
    assert merged not in {event.commit_hash for event in await _events(real_db_session, project)}

    assert merge.stdin is not None
    merge.stdin.write("\n")
    merge.stdin.flush()
    assert (await _line(merge))["step"] == "committed"
    await _finish(merge)

    reconciler = _start("once", tmp_path, project)
    released = await _line(reconciler)
    await _finish(reconciler)
    assert (released["processed"], released["recorded"], released["more"]) == (1, 1, False)
    (event,) = [
        event for event in await _events(real_db_session, project) if event.commit_hash == merged
    ]
    assert (event.user_id, event.user_name, event.origin) == ("contributor", "Contributor", "human")
    assert (
        await real_db_session.scalar(
            select(func.count())
            .select_from(EntityChangeEvent)
            .where(EntityChangeEvent.project_id == project)
        )
        == 2
    )
