"""Real-PostgreSQL proof that paging a feed shows the first page's history throughout."""

from __future__ import annotations

from base64 import b64decode
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ontokit.api.routes.analytics import _begin_snapshot
from ontokit.models.change_event import EntityChangeEvent
from ontokit.models.project import Project
from ontokit.schemas.analytics import ProjectChangesResponse
from ontokit.services.change_event_service import ChangeEventService

pytestmark = pytest.mark.integration

IRI = "https://example.org/ontology#E{:02d}"
NOW = datetime.now(UTC)


@pytest_asyncio.fixture
async def project(real_db_session: AsyncSession) -> AsyncIterator[UUID]:
    project_id = uuid4()
    real_db_session.add(Project(id=project_id, name="Paging test ontology", owner_id="owner"))
    await real_db_session.commit()
    try:
        yield project_id
    finally:
        await real_db_session.rollback()
        await real_db_session.execute(delete(Project).where(Project.id == project_id))
        await real_db_session.commit()


def _event(project: UUID, entity: int, hours_ago: float, **values: object) -> EntityChangeEvent:
    fields: dict[str, object] = {
        "project_id": project,
        "branch": "main",
        "entity_iri": IRI.format(entity),
        "entity_type": "class",
        "event_type": "update",
        "user_id": "owner",
        "changed_fields": ["labels"],
        "old_values": {"labels": [f"Before {entity}"]},
        "new_values": {"labels": [f"After {entity} at {hours_ago}"]},
        # Reconciled history carries its commit time, however long ago that was.
        "created_at": NOW - timedelta(hours=hours_ago),
    }
    return EntityChangeEvent(**(fields | values))


@pytest.mark.asyncio
async def test_pages_read_while_history_is_written_show_the_first_pages_truth(
    real_db_session: AsyncSession, project: UUID
) -> None:
    sessions = async_sessionmaker(real_db_session.bind, expire_on_commit=False)

    async def read(cursor: str | None = None, limit: int = 10) -> ProjectChangesResponse:
        async with sessions() as reader:
            await _begin_snapshot(reader)
            return await ChangeEventService(reader).list_changes(
                project, branch="main", limit=limit, cursor=cursor
            )

    # Entity n last changed n hours ago, so E00..E09 fill page one and E20..E29 page three.
    async with sessions() as writer:
        writer.add_all(_event(project, entity, entity + 1) for entity in range(30))
        await writer.commit()
    truth = await read(limit=100)
    assert [item.entity_iri for item in truth.items] == [IRI.format(n) for n in range(30)]

    # A backfill that began before the first page and commits after it.
    in_flight = sessions()
    in_flight.add(_event(project, 25, 0.5))
    await in_flight.flush()
    async with sessions() as other:
        # A later transaction finishes first, so the first page's snapshot lists
        # the backfill as running rather than placing it past its horizon.
        await other.execute(text("SELECT pg_current_xact_id()"))
        await other.commit()

    first = await read()
    assert first.next_cursor is not None
    snapshot = b64decode(first.next_cursor, altchars=b"-_").decode().split("|")[2]
    assert snapshot.split(":")[2], snapshot

    await in_flight.commit()
    await in_flight.close()
    async with sessions() as writer:
        writer.add_all(
            [
                # Moves a later entity ahead of the cursor, where later pages would skip it.
                _event(project, 22, 0.25),
                # Old history for an entity on a shown page and one on a later page.
                _event(project, 3, 14.5, event_type="create"),
                _event(project, 12, 40, event_type="create"),
                # New entities that would push later pages' entities along.
                _event(project, 40, 15.5),
                _event(project, 41, 27.5),
            ]
        )
        await writer.commit()

    pages = [first]
    while pages[-1].next_cursor is not None:
        pages.append(await read(pages[-1].next_cursor))

    seen = [item for page in pages for item in page.items]
    assert [item.entity_iri for item in seen] == [item.entity_iri for item in truth.items]
    assert [item.model_dump() for item in seen] == [item.model_dump() for item in truth.items]
    assert len(pages) == 3
    assert all(page.summary == truth.summary for page in pages)
    assert all(page.window == pages[0].window for page in pages)

    # A fresh read sees the settled history.
    settled = await read(limit=100)
    assert settled.summary.entities_changed == 32
    assert settled.items[0].entity_iri == IRI.format(22)
    assert settled.summary.total_events == truth.summary.total_events + 6
