"""Pin the feed cursor: it carries its first page's snapshot and rejects tampering."""

from __future__ import annotations

from base64 import b64decode, urlsafe_b64encode
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from ontokit.models.change_event import EntityChangeEvent
from ontokit.services.change_event_service import (
    ChangeEventService,
    InvalidChangesCursor,
    _Snapshot,
    _write_cursor,
)

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


def _row(number: int, recorded_xact: int | None) -> dict[str, object]:
    return {
        "id": UUID(int=number),
        "project_id": PROJECT,
        "branch": "trunk",
        "entity_iri": f"{BASE_IRI}E{number:02d}",
        "entity_type": "class",
        "event_type": "update",
        "user_id": "user-a",
        "origin": "human",
        "changed_fields": [],
        "created_at": datetime.now(UTC) - timedelta(hours=number),
        "recorded_xact": recorded_xact,
    }


def _encode(*fields: str) -> str:
    return urlsafe_b64encode("|".join(fields).encode()).decode()


@pytest.mark.asyncio
async def test_later_pages_keep_only_events_their_snapshot_saw(feed) -> None:
    service, connection = feed
    # Recorded before the column existed, before the horizon, running, after it.
    xacts = [None, 5, 13, 12, 15, 20, 25]
    connection.execute(TABLE.insert(), [_row(n, x) for n, x in enumerate(xacts, 1)])
    until = datetime.now(UTC)
    snapshot = _Snapshot.from_postgresql("10:20:12,15")
    # A cursor ahead of every entity, so the page holds whatever the snapshot saw.
    cursor = _write_cursor(until, until, snapshot, BASE_IRI)

    page = await service.list_changes(PROJECT, branch="trunk", cursor=cursor)

    assert [item.entity_iri.removeprefix(BASE_IRI) for item in page.items] == [
        "E01",
        "E02",
        "E03",
    ]
    assert page.summary.total_events == 3
    assert page.window.until == until.isoformat()


@pytest.mark.asyncio
async def test_next_cursor_carries_the_same_snapshot_and_window(feed) -> None:
    service, connection = feed
    connection.execute(TABLE.insert(), [_row(n, None) for n in range(1, 4)])
    until = datetime.now(UTC)
    snapshot = _Snapshot.from_postgresql("1000:1010:1003,1007")
    cursor = _write_cursor(until, until, snapshot, BASE_IRI)

    page = await service.list_changes(PROJECT, branch="trunk", limit=1, cursor=cursor)

    assert page.next_cursor is not None
    window_end, _, carried, iri = b64decode(page.next_cursor, altchars=b"-_").decode().split("|")
    assert (window_end, carried, iri) == (until.isoformat(), "1000:10:3,7", BASE_IRI + "E01")
    assert snapshot.to_cursor() == "1000:10:3,7"
    assert _Snapshot.from_cursor(snapshot.to_cursor()) == snapshot


@pytest.mark.parametrize(
    "snapshot",
    [
        "",
        "abc",
        "10:20",
        "0:0:",  # No transaction id is zero.
        "10:-5:",
        "10:5:5",  # A running transaction at or past the horizon.
        "10:5:1,9",
        "10:5:,",
        f"{2**63}:0:",
        "10:5:1 ",
    ],
)
@pytest.mark.asyncio
async def test_a_tampered_snapshot_is_rejected(feed, snapshot: str) -> None:
    service, _ = feed
    until = datetime.now(UTC).isoformat()

    with pytest.raises(InvalidChangesCursor):
        await service.list_changes(
            PROJECT, branch="trunk", cursor=_encode(until, until, snapshot, BASE_IRI + "E01")
        )


@pytest.mark.asyncio
async def test_a_cursor_without_a_snapshot_is_rejected(feed) -> None:
    service, _ = feed
    until = datetime.now(UTC).isoformat()

    with pytest.raises(InvalidChangesCursor):
        await service.list_changes(
            PROJECT, branch="trunk", cursor=_encode(until, until, BASE_IRI + "E01")
        )
