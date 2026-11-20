"""Real-PostgreSQL proof that a change feed response is internally consistent."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ontokit.api.routes.analytics import _begin_snapshot
from ontokit.models.change_event import EntityChangeEvent
from ontokit.models.project import Project
from ontokit.services.change_event_service import ChangeEventService

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def project(real_db_session: AsyncSession) -> AsyncIterator[UUID]:
    project_id = uuid4()
    real_db_session.add(Project(id=project_id, name="Snapshot test ontology", owner_id="owner"))
    await real_db_session.commit()
    try:
        yield project_id
    finally:
        await real_db_session.rollback()
        await real_db_session.execute(delete(Project).where(Project.id == project_id))
        await real_db_session.commit()


def _events(project: UUID, prefix: str, count: int) -> list[EntityChangeEvent]:
    return [
        EntityChangeEvent(
            project_id=project,
            branch="main",
            entity_iri=f"https://example.org/ontology#{prefix}{number}",
            entity_type="class",
            event_type="create",
            user_id="owner",
            new_values={"labels": [f"{prefix} {number}"]},
            # Reconciled history carries its commit time, which falls inside the window.
            created_at=datetime.now(UTC) - timedelta(hours=1),
        )
        for number in range(count)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot", [False, True])
async def test_feed_page_and_summary_agree_while_history_is_being_recorded(
    real_db_session: AsyncSession,
    project: UUID,
    monkeypatch: pytest.MonkeyPatch,
    snapshot: bool,
) -> None:
    writer_sessions = async_sessionmaker(real_db_session.bind, expire_on_commit=False)
    async with writer_sessions() as writer:
        writer.add_all(_events(project, "Existing", 3))
        await writer.commit()

    service = ChangeEventService(real_db_session)
    read_summary = service._changes_summary

    async def backfill_then_read_summary(*arguments: object) -> object:
        # A reconciler commits more history between the page and summary queries.
        async with writer_sessions() as writer:
            writer.add_all(_events(project, "Backfilled", 5))
            await writer.commit()
        return await read_summary(*arguments)  # type: ignore[arg-type]

    monkeypatch.setattr(service, "_changes_summary", backfill_then_read_summary)
    if snapshot:
        await _begin_snapshot(real_db_session)
    response = await service.list_changes(project, branch="main")

    assert len(response.items) == 3
    if snapshot:
        assert (response.summary.entities_changed, response.summary.total_events) == (3, 3)
    else:
        # Without the snapshot the same response counts rows its page never saw.
        assert (response.summary.entities_changed, response.summary.total_events) == (8, 8)

    monkeypatch.setattr(service, "_changes_summary", read_summary)
    await _begin_snapshot(real_db_session)
    later = await service.list_changes(project, branch="main")
    assert (len(later.items), later.summary.entities_changed) == (8, 8)
    markers = await service.list_changed_entities(project, branch="main")
    assert len(markers.items) == 8


@pytest.mark.asyncio
async def test_snapshot_isolation_does_not_reach_the_next_request() -> None:
    """The pooled connection returns to the default level for whoever borrows it next."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL not set")
    engine = create_async_engine(database_url, pool_size=1, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as feed_request:
            await feed_request.execute(text("SELECT 1"))  # The access check.
            await _begin_snapshot(feed_request)
            level = await feed_request.scalar(text("SHOW transaction_isolation"))
            process = await feed_request.scalar(text("SELECT pg_backend_pid()"))
        async with sessions() as next_request:
            assert await next_request.scalar(text("SELECT pg_backend_pid()")) == process
            next_level = await next_request.scalar(text("SHOW transaction_isolation"))
    finally:
        await engine.dispose()

    assert (level, next_level) == ("repeatable read", "read committed")
