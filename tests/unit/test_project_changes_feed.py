"""Exercise the change feeds with real grouped SQL on a fixture event table."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from ontokit.models.change_event import EntityChangeEvent
from ontokit.services.change_event_service import ChangeEventService

PROJECT = UUID(int=1)
BASE_IRI = "https://example.org/ontology#"
TABLE = sa.inspect(EntityChangeEvent).local_table


@pytest.fixture
def feed() -> Iterator[tuple[ChangeEventService, Connection]]:
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection, Session(bind=connection) as session:
        TABLE.create(connection)
        db = AsyncMock()
        db.execute.side_effect = session.execute
        yield ChangeEventService(db), connection
    engine.dispose()


def _event(iri: str, age: int, event_type: str = "update", **values: object) -> dict[str, object]:
    return {
        "id": UUID(int=0),
        "project_id": PROJECT,
        "branch": "trunk",
        "entity_iri": BASE_IRI + iri,
        "entity_type": "class",
        "event_type": event_type,
        "user_id": "user-a",
        "user_name": "Editor A",
        "origin": "human",
        "changed_fields": [],
        "old_values": None,
        "new_values": None,
        "created_at": datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        - timedelta(days=age),
        **values,
    }


def _insert(connection: Connection, rows: list[dict[str, object]]) -> None:
    connection.execute(TABLE.insert(), [dict(row, id=UUID(int=i)) for i, row in enumerate(rows, 1)])


def _example_events() -> list[dict[str, object]]:
    return [
        _event(
            "Added",
            3,
            "create",
            origin="ai_edited",
            new_values={"labels": ["Draft"], "definitions": ["Original"], "parents": []},
        ),
        _event(
            "Added",
            0,
            "rename",
            changed_fields=["labels", "definitions"],
            old_values={"labels": ["Draft"], "definitions": ["Original"]},
            new_values={"labels": ["Final"], "definitions": ["Revised"]},
        ),
        _event(
            "Modified",
            3,
            changed_fields=["definitions", "comments", "deprecated"],
            entity_type="property",
            user_name="Earlier editor name",
            old_values={"definitions": ["First"], "comments": [], "deprecated": False},
            new_values={"definitions": ["Second"], "comments": ["Temporary"], "deprecated": True},
        ),
        _event(
            "Modified",
            0,
            changed_fields=["definitions", "comments"],
            entity_type="property",
            origin="ai",
            user_id="user-b",
            user_name=None,
            old_values={"definitions": ["Second"], "comments": ["Temporary"]},
            new_values={"definitions": ["Third"], "comments": []},
        ),
        _event("Removed", 2, "delete", origin="system", old_values={"labels": ["Removed label"]}),
        _event("ShortLived", 4, "create", new_values={"labels": ["Transient"]}),
        _event("ShortLived", 1, "delete", old_values={"labels": ["Transient"]}),
        _event("Old", 14),
        _event("OtherBranch", 0, branch="review"),
        _event("OtherProject", 0, project_id=UUID(int=2)),
    ]


@pytest.mark.asyncio
async def test_groups_entities_and_paginates_a_stable_window(feed) -> None:
    service, connection = feed
    _insert(connection, _example_events())

    first = await service.list_changes(PROJECT, branch="trunk", limit=1)
    assert first.window.days == 7
    assert datetime.fromisoformat(first.window.since).utcoffset() == timedelta(0)
    assert datetime.fromisoformat(first.window.until) - datetime.fromisoformat(
        first.window.since
    ) == (timedelta(days=7))
    assert first.summary.model_dump() == {
        "total_events": 7,
        "entities_changed": 4,
        "by_net_change": {"added": 1, "modified": 1, "removed": 1, "transient": 1},
        "by_origin": {"human": 4, "ai": 1, "ai_edited": 1, "system": 1},
        "authors": [
            {"user_id": "user-a", "user_name": "Editor A", "event_count": 6},
            {"user_id": "user-b", "user_name": None, "event_count": 1},
        ],
    }
    assert len(first.items) == 1
    added = first.items[0]
    assert added.entity_iri == BASE_IRI + "Added"
    assert added.label == "Final"
    assert added.net_change == "added"
    assert set(added.origins) == {"human", "ai_edited"}
    assert added.event_count == 2
    assert [e.event_type for e in added.events] == ["rename", "create"]
    assert [e.origin for e in added.events] == ["human", "ai_edited"]
    assert [(f.field, f.before, f.after) for f in added.field_changes] == [
        ("labels", [], ["Final"]),
        ("definitions", [], ["Revised"]),
    ]

    # A new event after the first request must not enter the window mid-pagination.
    later_row = _event("Later", 0)
    connection.execute(
        TABLE.insert(),
        dict(later_row, id=UUID(int=50), created_at=datetime.now(UTC) + timedelta(microseconds=1)),
    )
    page = first
    items = list(page.items)
    while page.next_cursor is not None and len(items) <= 4:
        page = await service.list_changes(PROJECT, branch="trunk", limit=1, cursor=page.next_cursor)
        assert page.window == first.window
        assert page.summary == first.summary
        items.extend(page.items)
    assert page.next_cursor is None
    assert [(i.entity_iri.removeprefix(BASE_IRI), i.net_change) for i in items] == [
        ("Added", "added"),
        ("Modified", "modified"),
        ("ShortLived", "transient"),
        ("Removed", "removed"),
    ]
    _, modified, short_lived, removed = items
    assert [(f.field, f.before, f.after) for f in modified.field_changes] == [
        ("definitions", ["First"], ["Third"]),
        ("deprecated", False, True),
    ]
    assert {a.user_id: a.user_name for a in modified.authors} == {
        "user-a": "Earlier editor name",
        "user-b": None,
    }
    assert modified.label is None
    assert short_lived.field_changes == []
    assert removed.label == "Removed label"
    assert [(f.field, f.before, f.after) for f in removed.field_changes] == [
        ("labels", ["Removed label"], []),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filters,expected,count",
    [
        ({"origin": ["ai", "ai_edited"]}, {"Added": "added", "Modified": "modified"}, 4),
        ({"author": ["user-b"]}, {"Modified": "modified"}, 2),
        ({"entity_type": ["property"]}, {"Modified": "modified"}, 2),
        ({"event_type": ["create"]}, {"Added": "added", "ShortLived": "transient"}, 4),
        ({"event_type": ["rename"], "origin": ["ai_edited"]}, {}, 0),
        (
            {"days": 2},
            {"Added": "modified", "Modified": "modified", "ShortLived": "removed"},
            3,
        ),
        ({"branch": "review"}, {"OtherBranch": "modified"}, 1),
        ({"origin": ["system"], "entity_type": ["individual"]}, {}, 0),
    ],
)
async def test_filters_select_entities_and_keep_their_window_events(
    feed, filters, expected, count
) -> None:
    service, connection = feed
    _insert(connection, _example_events())

    response = await service.list_changes(PROJECT, **({"branch": "trunk"} | filters))
    assert {item.entity_iri.removeprefix(BASE_IRI): item.net_change for item in response.items} == (
        expected
    )
    assert response.summary.total_events == count


@pytest.mark.asyncio
async def test_author_filter_uses_later_edits_by_other_authors(feed) -> None:
    service, connection = feed
    _insert(
        connection,
        [
            _event(
                "Renamed",
                2,
                "rename",
                changed_fields=["labels"],
                old_values={"labels": ["Original"]},
                new_values={"labels": ["Draft"]},
            ),
            _event(
                "Renamed",
                1,
                "rename",
                origin="system",
                user_id="user-b",
                user_name="Editor B",
                changed_fields=["labels"],
                old_values={"labels": ["Draft"]},
                new_values={"labels": ["Final"]},
            ),
            _event("Unmatched", 0, user_id="user-b", user_name="Editor B"),
        ],
    )

    response = await service.list_changes(PROJECT, branch="trunk", author=["user-a"])

    (item,) = response.items
    assert item.entity_iri == BASE_IRI + "Renamed"
    assert [(f.field, f.before, f.after) for f in item.field_changes] == [
        ("labels", ["Original"], ["Final"]),
    ]
    assert item.label == "Final"
    assert item.origins == ["human", "system"]
    assert [author.user_id for author in item.authors] == ["user-b", "user-a"]
    assert item.event_count == 2
    assert [event.user_id for event in item.events] == ["user-b", "user-a"]
    assert item.last_changed_at == item.events[0].created_at
    assert response.summary.model_dump() == {
        "total_events": 2,
        "entities_changed": 1,
        "by_net_change": {"added": 0, "modified": 1, "removed": 0, "transient": 0},
        "by_origin": {"human": 1, "ai": 0, "ai_edited": 0, "system": 1},
        "authors": [
            {"user_id": "user-a", "user_name": "Editor A", "event_count": 1},
            {"user_id": "user-b", "user_name": "Editor B", "event_count": 1},
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_types,event_filter,net_change",
    [
        pytest.param(["delete", "create"], ["delete"], "modified", id="delete-recreate"),
        pytest.param(["create", "delete"], None, "transient", id="create-delete"),
        pytest.param(["create", "delete", "create"], None, "added", id="create-delete-recreate"),
    ],
)
async def test_net_change_uses_existence_at_window_edges(
    feed, event_types, event_filter, net_change
) -> None:
    service, connection = feed
    _insert(
        connection,
        [_event("Entity", 3 - i, event_type) for i, event_type in enumerate(event_types)],
    )

    response = await service.list_changes(PROJECT, branch="trunk", event_type=event_filter)

    (item,) = response.items
    assert item.net_change == net_change
    assert response.summary.by_net_change[net_change] == 1
    assert response.summary.by_net_change["removed"] == 0


@pytest.mark.asyncio
async def test_item_uses_earlier_events_beyond_the_twenty_displayed(feed) -> None:
    service, connection = feed
    _insert(
        connection,
        [
            _event(
                "LongHistory", 2, "create", new_values={"labels": ["Known"], "definitions": ["A"]}
            ),
            _event(
                "LongHistory",
                1,
                changed_fields=["definitions"],
                old_values={"definitions": ["A"]},
                new_values={"definitions": ["B"]},
            ),
            *[_event("LongHistory", 0, changed_fields=["other"]) for _ in range(20)],
        ],
    )
    (item,) = (await service.list_changes(PROJECT, branch="trunk")).items
    assert item.label == "Known"
    assert item.net_change == "added"
    assert item.event_count == 22
    assert len(item.events) == 20
    assert all(event.changed_fields == ["other"] for event in item.events)
    assert [(f.field, f.before, f.after) for f in item.field_changes] == [
        ("labels", [], ["Known"]),
        ("definitions", [], ["B"]),
    ]


@pytest.mark.asyncio
async def test_changed_entities_returns_only_markers_and_caps_entities(feed) -> None:
    service, connection = feed
    _insert(
        connection,
        [
            *[_event(f"Entity{i:04d}", 1, "create", origin="ai") for i in range(5001)],
            _event("Newest", 0, "delete", origin="system"),
            _event("Newest", 1, "create", origin="ai"),
            _event("Review", 0, "create", origin="ai", branch="review"),
        ],
    )

    markers = await service.list_changed_entities(PROJECT, branch="trunk")
    assert len(markers.items) == 5000
    assert markers.truncated
    first = markers.items[0].model_dump()
    assert set(first) == {"entity_iri", "net_change", "origins", "last_changed_at"}
    assert first["entity_iri"] == BASE_IRI + "Newest"
    assert first["net_change"] == "transient"
    assert set(first["origins"]) == {"ai", "system"}
    assert markers.items[1].entity_iri == BASE_IRI + "Entity0000"
    assert markers.items[-1].entity_iri == BASE_IRI + "Entity4998"

    filtered = await service.list_changed_entities(PROJECT, branch="trunk", origin=["ai"])
    assert filtered.truncated
    assert filtered.items[0].model_dump() == first


@pytest.mark.asyncio
async def test_other_reports_untracked_changes_only_for_entities_present_at_both_edges(
    feed,
) -> None:
    service, connection = feed
    _insert(
        connection,
        [
            _event("Annotated", 2, changed_fields=["other"]),
            _event(
                "Relabeled",
                3,
                changed_fields=["labels", "other"],
                old_values={"labels": ["Draft"]},
                new_values={"labels": ["Final"]},
            ),
            _event(
                "Relabeled",
                1,
                "rename",
                changed_fields=["labels"],
                old_values={"labels": ["Final"]},
                new_values={"labels": ["Draft"]},
            ),
            _event("Added", 3, "create", new_values={"labels": ["Added"]}),
            _event("Added", 1, changed_fields=["other"]),
            _event("Removed", 3, changed_fields=["other"]),
            _event("Removed", 1, "delete", old_values={"labels": ["Removed"]}),
        ],
    )

    response = await service.list_changes(PROJECT, branch="trunk")

    other = {"field": "other", "before": None, "after": None}
    fields = {
        item.entity_iri.removeprefix(BASE_IRI): [
            change.model_dump() for change in item.field_changes
        ]
        for item in response.items
    }
    # A net-zero label edit is omitted; the value-less flag stays.
    assert fields["Annotated"] == [other]
    assert fields["Relabeled"] == [other]
    assert other not in fields["Added"]
    assert other not in fields["Removed"]
