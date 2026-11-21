"""Real-PostgreSQL proof that bounded item reads keep each item's net change exact."""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.models.change_event import EntityChangeEvent
from ontokit.models.project import Project
from ontokit.services import change_event_service
from ontokit.services.change_event_service import ChangeEventService
from tests.unit.test_project_changes_feed_scale import _random_history

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def project(real_db_session: AsyncSession) -> AsyncIterator[UUID]:
    project_id = uuid4()
    real_db_session.add(Project(id=project_id, name="Feed scale test ontology", owner_id="owner"))
    await real_db_session.commit()
    try:
        yield project_id
    finally:
        await real_db_session.rollback()
        await real_db_session.execute(delete(Project).where(Project.id == project_id))
        await real_db_session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", range(4))
async def test_items_equal_the_items_built_from_every_event(
    real_db_session: AsyncSession, project: UUID, seed: int
) -> None:
    rows = [
        dict(row, id=uuid4(), project_id=project)
        for row in _random_history(random.Random(seed), 12)
    ]
    await real_db_session.execute(insert(EntityChangeEvent), rows)
    await real_db_session.commit()

    response = await ChangeEventService(real_db_session).list_changes(
        project, branch="trunk", limit=100
    )

    event = EntityChangeEvent
    history: dict[str, list[EntityChangeEvent]] = defaultdict(list)
    query = (
        select(event)
        .where(event.project_id == project)
        .order_by(event.created_at.desc(), event.id.desc())
    )
    for row in (await real_db_session.execute(query)).scalars():
        history[row.entity_iri].append(row)
    assert {item.entity_iri for item in response.items} == history.keys()
    for item in response.items:
        events = history[item.entity_iri]
        expected = change_event_service._project_change_item(
            item.entity_iri, item.net_change, events, len(events)
        )
        assert item.model_dump() == expected.model_dump(), item.entity_iri
