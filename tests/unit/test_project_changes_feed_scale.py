"""Pin that a heavily edited entity's feed item reads a bounded set of its events."""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from ontokit.models.change_event import EntityChangeEvent
from ontokit.services import change_event_service
from ontokit.services.change_event_service import ChangeEventService

PROJECT = UUID(int=1)
BASE_IRI = "https://example.org/ontology#"
TABLE = sa.inspect(EntityChangeEvent).local_table
FIELDS = ["labels", "definitions", "parents", "deprecated", "comments", "other"]
NOW = datetime.now(UTC).replace(microsecond=0)


class _Feed:
    def __init__(self, session: Session, connection: Connection) -> None:
        self.session = session
        self.connection = connection
        self.rows_fetched: list[int] = []

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        frozen = self.session.execute(*args, **kwargs).freeze()
        self.rows_fetched.append(len(frozen.data))
        return frozen()


@pytest.fixture
def feed() -> Iterator[tuple[ChangeEventService, _Feed]]:
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection, Session(bind=connection) as session:
        TABLE.create(connection)
        recorder = _Feed(session, connection)
        db = AsyncMock()
        db.execute.side_effect = recorder.execute
        yield ChangeEventService(db), recorder
    engine.dispose()


def _row(number: int, iri: str, seconds_ago: int, **values: object) -> dict[str, object]:
    return {
        "id": UUID(int=number),
        "project_id": PROJECT,
        "branch": "trunk",
        "entity_iri": BASE_IRI + iri,
        "entity_type": "class",
        "event_type": "update",
        "user_id": "user-a",
        "user_name": "Editor A",
        "origin": "human",
        "changed_fields": [],
        "old_values": None,
        "new_values": None,
        "created_at": NOW - timedelta(seconds=seconds_ago),
        **values,
    }


def _value(rng: random.Random, field: str) -> object:
    if field == "deprecated":
        return rng.choice([True, False])
    return rng.choice([[], ["A"], ["B"], ["A", "B"]])


def _values(rng: random.Random, rare: float) -> dict[str, object] | None:
    if rng.random() < 0.15:
        return None
    return {
        field: _value(rng, field)
        for field in FIELDS[:-1] + ["unrelated"]
        if rng.random() < (rare if field == "labels" else 0.4)
    }


def _random_history(rng: random.Random, entities: int) -> list[dict[str, object]]:
    rows = []
    number = 0
    for entity in range(entities):
        # Some histories hold a label, an origin or an author in only one old event.
        rare = rng.choice([0.4, 0.02])
        for _ in range(rng.randint(1, 90)):
            number += 1
            if rare < 0.1 and rng.random() < 0.9:
                # An edit that touches no tracked field, so field edges sit deep in history.
                rows.append(_row(number, f"E{entity}", rng.randint(0, 40) * 3600))
                continue
            rows.append(
                _row(
                    number,
                    f"E{entity}",
                    # Coarse timestamps make ties, which the event id breaks.
                    rng.randint(0, 40) * 3600,
                    event_type=rng.choice(
                        ["create", "update", "rename", "reparent", "deprecate", "delete"]
                    ),
                    user_id=rng.choice(["user-a", "user-b"]) if rng.random() > rare else "user-c",
                    user_name=rng.choice(["Editor", "Renamed editor", None, None]),
                    origin="human" if rng.random() > rare else rng.choice(["ai", "system"]),
                    changed_fields=rng.sample(FIELDS, rng.randint(0, 3)),
                    old_values=_values(rng, rare),
                    new_values=_values(rng, rare),
                )
            )
    return rows


def _reference_items(feed: _Feed, net_changes: dict[str, str]) -> dict[str, dict[str, object]]:
    """Build each item from every one of its events, the way the feed defines it."""
    event = EntityChangeEvent
    events: dict[str, list[EntityChangeEvent]] = defaultdict(list)
    query = sa.select(event).order_by(event.created_at.desc(), event.id.desc())
    for row in feed.session.execute(query).scalars():
        events[row.entity_iri].append(row)
    return {
        iri: change_event_service._project_change_item(
            iri,
            net_changes[iri],  # type: ignore[arg-type]
            history,
            len(history),
        ).model_dump()
        for iri, history in events.items()
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", range(20))
async def test_items_equal_the_items_built_from_every_event(feed, seed: int) -> None:
    service, recorder = feed
    recorder.connection.execute(TABLE.insert(), _random_history(random.Random(seed), 12))

    response = await service.list_changes(PROJECT, branch="trunk", limit=100)

    actual = {item.entity_iri: item.model_dump() for item in response.items}
    reference = _reference_items(
        recorder, {item.entity_iri: item.net_change for item in response.items}
    )
    assert actual.keys() == reference.keys()
    for iri, item in actual.items():
        assert item == reference[iri], iri


@pytest.mark.asyncio
async def test_a_heavily_edited_entity_does_not_load_its_whole_window(feed) -> None:
    service, recorder = feed
    rows = [
        _row(1, "Busy", 5000, event_type="create", new_values={"labels": ["Start"]}),
        *[
            _row(
                number,
                "Busy",
                5000 - number,
                changed_fields=["labels", "other"],
                old_values={"labels": [f"L{number - 1}"]},
                new_values={"labels": [f"L{number}"]},
            )
            for number in range(2, 601)
        ],
        _row(700, "Quiet", 10, changed_fields=["comments"], new_values={"comments": ["x"]}),
    ]
    recorder.connection.execute(TABLE.insert(), rows)

    response = await service.list_changes(PROJECT, branch="trunk")

    busy = next(item for item in response.items if item.entity_iri == BASE_IRI + "Busy")
    assert busy.event_count == 600
    assert busy.net_change == "added"
    assert busy.label == "L600"
    assert [(f.field, f.before, f.after) for f in busy.field_changes] == [
        ("labels", [], ["L600"]),
    ]
    assert len(busy.events) == 20
    # Grouping and summary rows are one per entity, origin or author; no statement
    # may return the busy entity's whole history.
    assert max(recorder.rows_fetched) < 100, recorder.rows_fetched
