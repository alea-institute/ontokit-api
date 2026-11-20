"""Change event service — track entity modifications from graph diffs."""

import logging
from base64 import b64decode, urlsafe_b64encode
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import chain
from typing import Literal, cast
from uuid import UUID

from rdflib import BNode, Graph, URIRef
from rdflib import Literal as RDFLiteral
from rdflib.compare import isomorphic
from rdflib.namespace import OWL, RDF, RDFS, SKOS
from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement
from sqlalchemy.sql.selectable import CTE

from ontokit.models.change_event import (
    MAX_ENTITY_IRI_LENGTH,
    ChangeEventType,
    ChangeOrigin,
    EntityChangeEvent,
)
from ontokit.schemas.analytics import (
    ActivityDay,
    ChangeAuthor,
    ChangeAuthorCount,
    ChangedEntitiesResponse,
    ChangedEntity,
    ChangeEvent,
    ChangeField,
    ChangeFieldValue,
    ChangesSummary,
    ChangesWindow,
    ContributorStats,
    EntityHistoryResponse,
    FieldChange,
    HotEntity,
    NetChange,
    ProjectActivity,
    ProjectChangeItem,
    ProjectChangesResponse,
    TopEditor,
)
from ontokit.services.rdf_utils import get_entity_type as _get_entity_type
from ontokit.services.rdf_utils import is_deprecated as _is_deprecated

logger = logging.getLogger(__name__)

OriginResolver = Callable[
    [UUID, dict[str, dict[str, list[str]]]], Awaitable[dict[str, dict[str, str | None]]]
]
_CHANGE_FIELDS: tuple[ChangeField, ...] = (
    "labels",
    "definitions",
    "parents",
    "deprecated",
    "comments",
    "other",
)
_MARKER_LIMIT = 5000
_ITEM_EVENT_LIMIT = 20
_EVENT_INSERT_BATCH_SIZE = 1000


class InvalidChangesCursor(ValueError):
    """A project changes cursor could not be decoded."""


@dataclass
class ChangeEventSkips:
    overlong_iris: int = 0

    def warn(self) -> None:
        if self.overlong_iris:
            logger.warning(
                "Skipped %d change events with entity IRIs longer than %d characters",
                self.overlong_iris,
                MAX_ENTITY_IRI_LENGTH,
            )


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _clamp_occurrence_time(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("occurred_at must be timezone-aware")
    return min(value.astimezone(UTC), datetime.now(UTC))


def _read_cursor(cursor: str, days: int, now: datetime) -> tuple[datetime, datetime, str]:
    try:
        window_end, last_changed_at, iri = (
            b64decode(cursor, altchars=b"-_", validate=True).decode().split("|", 2)
        )
        until = datetime.fromisoformat(window_end)
        timestamp = datetime.fromisoformat(last_changed_at)
        since = until - timedelta(days=days)
        if (
            until.utcoffset() != timedelta(0)
            or timestamp.utcoffset() != timedelta(0)
            or not since <= timestamp <= until <= now
            or not iri
            or len(iri) > MAX_ENTITY_IRI_LENGTH
            or "\x00" in iri
        ):
            raise ValueError
    except (ValueError, OverflowError) as exc:
        raise InvalidChangesCursor("Invalid changes cursor") from exc
    return until, timestamp, iri


def _serialize_event(row: EntityChangeEvent) -> ChangeEvent:
    return ChangeEvent(
        id=str(row.id),
        project_id=str(row.project_id),
        branch=row.branch,
        entity_iri=row.entity_iri,
        entity_type=row.entity_type,
        event_type=cast(
            Literal["create", "update", "delete", "rename", "reparent", "deprecate"],
            row.event_type,
        ),
        user_id=row.user_id,
        user_name=row.user_name,
        commit_hash=row.commit_hash,
        origin=ChangeOrigin(row.origin),
        origin_model=row.origin_model,
        origin_batch_id=str(row.origin_batch_id) if row.origin_batch_id is not None else None,
        changed_fields=row.changed_fields or [],
        old_values=row.old_values,
        new_values=row.new_values,
        created_at=_utc_iso(row.created_at),
    )


def _event_filters(
    project_id: UUID,
    branch: str,
    since: datetime,
    until: datetime,
    *,
    origin: Sequence[str] | None = None,
    author: list[str] | None = None,
    entity_type: list[str] | None = None,
    event_type: list[str] | None = None,
) -> list[ColumnElement[bool]]:
    event = EntityChangeEvent
    filters = [
        event.project_id == project_id,
        event.branch == branch,
        event.created_at.between(since, until),
    ]
    if entity_type:
        filters.append(event.entity_type.in_(entity_type))

    entity_filters = [
        column.in_(values)
        for column, values in (
            (event.origin, origin),
            (event.user_id, author),
            (event.event_type, event_type),
        )
        if values
    ]
    if entity_filters:
        # One matching event qualifies the entity. Keep its other events in the window.
        matching_iris = select(event.entity_iri).where(*filters, *entity_filters)
        filters.append(event.entity_iri.in_(matching_iris))
    return filters


def _group_changes(filters: list[ColumnElement[bool]]) -> CTE:
    event = EntityChangeEvent
    newest_first = (event.created_at.desc(), event.id.desc())
    # The descending partition's last value is the earliest event in the filtered window.
    ranked = (
        select(
            event.entity_iri,
            event.created_at.label("last_changed_at"),
            event.event_type,
            func.last_value(event.event_type)
            .over(partition_by=event.entity_iri, order_by=newest_first, rows=(None, None))
            .label("first_event_type"),
            func.row_number()
            .over(partition_by=event.entity_iri, order_by=newest_first)
            .label("event_rank"),
        )
        .where(*filters)
        .subquery()
    ).c
    starts_with_create = ranked.first_event_type == ChangeEventType.CREATE
    ends_with_delete = ranked.event_type == ChangeEventType.DELETE
    net_change = case(
        (starts_with_create & ends_with_delete, "transient"),
        (starts_with_create, "added"),
        (ends_with_delete, "removed"),
        else_="modified",
    )
    return (
        select(ranked.entity_iri, ranked.last_changed_at, net_change.label("net_change"))
        .where(ranked.event_rank == 1)
        .cte("changed_entities")
    )


def _field_changes(events: list[EntityChangeEvent]) -> list[FieldChange]:
    before: dict[ChangeField, ChangeFieldValue] = {}
    after: dict[ChangeField, ChangeFieldValue] = {}
    for event in reversed(events):
        old, new = event.old_values or {}, event.new_values or {}
        touched = set(event.changed_fields or [])
        if event.event_type in (ChangeEventType.CREATE, ChangeEventType.DELETE):
            touched.update(old)
            touched.update(new)
        for field in _CHANGE_FIELDS:
            if field not in touched or field == "other":
                continue  # Untracked triples have no comparable before/after values.
            absent: ChangeFieldValue = False if field == "deprecated" else []
            before.setdefault(
                field, old.get(field, absent if event.event_type == "create" else None)
            )
            after[field] = new.get(field, absent if event.event_type == "delete" else None)

    return [
        FieldChange(field=field, before=before[field], after=after[field])
        for field in _CHANGE_FIELDS
        if field in before and before[field] != after[field]
    ]


def _project_change_item(
    entity_iri: str, net_change: NetChange, events: list[EntityChangeEvent]
) -> ProjectChangeItem:
    label = None
    authors: dict[str, str | None] = {}
    for event in events:
        if label is None:
            labels = (event.new_values or {}).get("labels") or (event.old_values or {}).get(
                "labels"
            )
            if labels:
                label = labels[0]
        if authors.get(event.user_id) is None:
            authors[event.user_id] = event.user_name
    latest = events[0]
    return ProjectChangeItem(
        entity_iri=entity_iri,
        entity_type=latest.entity_type,
        net_change=net_change,
        label=label,
        origins=sorted({ChangeOrigin(event.origin) for event in events}),
        last_changed_at=_utc_iso(latest.created_at),
        event_count=len(events),
        authors=[
            ChangeAuthor(user_id=user_id, user_name=name) for user_id, name in authors.items()
        ],
        field_changes=_field_changes(events),
        events=[_serialize_event(event) for event in events[:_ITEM_EVENT_LIMIT]],
    )


def _literal_values(graph: Graph, uri: URIRef, predicate: URIRef) -> list[str]:
    return sorted(str(o) for o in graph.objects(uri, predicate) if isinstance(o, RDFLiteral))


def _get_parents(graph: Graph, uri: URIRef, entity_type: str) -> list[str]:
    predicate = RDFS.subPropertyOf if entity_type == "property" else RDFS.subClassOf
    return sorted(str(p) for p in graph.objects(uri, predicate) if isinstance(p, URIRef))


def _entity_values(graph: Graph, uri: URIRef, entity_type: str) -> dict[str, list[str] | bool]:
    return {
        "labels": _literal_values(graph, uri, RDFS.label),
        "definitions": _literal_values(graph, uri, SKOS.definition),
        "parents": _get_parents(graph, uri, entity_type),
        "deprecated": _is_deprecated(graph, uri),
        "comments": _literal_values(graph, uri, RDFS.comment),
    }


def _entity_description(graph: Graph, uri: URIRef) -> Graph:
    """Collect the entity's outgoing statements and follow only blank nodes."""
    description = Graph()
    pending: list[URIRef | BNode] = [uri]
    seen: set[BNode] = set()
    while pending:
        for triple in graph.triples((pending.pop(), None, None)):
            description.add(triple)
            obj = triple[2]
            if isinstance(obj, BNode) and obj not in seen:
                seen.add(obj)
                pending.append(obj)
    return description


def _entity_statements_changed(old_graph: Graph, new_graph: Graph, uri: URIRef) -> bool:
    old_triples = set(old_graph.triples((uri, None, None)))
    new_triples = set(new_graph.triples((uri, None, None)))
    if any(isinstance(obj, BNode) for _, _, obj in chain(old_triples, new_triples)):
        return not isomorphic(
            _entity_description(old_graph, uri), _entity_description(new_graph, uri)
        )
    return old_triples != new_triples


def _get_declared_entities(graph: Graph) -> dict[str, str]:
    """Get {iri: entity_type} for all declared entities in graph."""
    entities: dict[str, str] = {}
    for s in graph.subjects(RDF.type, None):
        if not isinstance(s, URIRef) or s == OWL.Thing:
            continue
        etype = _get_entity_type(graph, s)
        if etype != "unknown":
            entities[str(s)] = etype
    return entities


class ChangeEventService:
    def __init__(self, db: AsyncSession):
        self._db = db

    async def record_event(
        self,
        project_id: UUID,
        branch: str,
        entity_iri: str,
        entity_type: str,
        event_type: str,
        user_id: str,
        user_name: str | None = None,
        commit_hash: str | None = None,
        changed_fields: list[str] | None = None,
        old_values: dict[str, object] | None = None,
        new_values: dict[str, object] | None = None,
        *,
        origin: str = ChangeOrigin.HUMAN,
        origin_model: str | None = None,
        origin_batch_id: UUID | None = None,
        occurred_at: datetime | None = None,
    ) -> EntityChangeEvent:
        event = EntityChangeEvent(
            project_id=project_id,
            branch=branch,
            entity_iri=entity_iri,
            entity_type=entity_type,
            event_type=event_type,
            user_id=user_id,
            user_name=user_name,
            commit_hash=commit_hash,
            origin=origin,
            origin_model=origin_model,
            origin_batch_id=origin_batch_id,
            changed_fields=changed_fields or [],
            old_values=old_values,
            new_values=new_values,
        )
        if occurred_at is not None:
            event.created_at = _clamp_occurrence_time(occurred_at)
        self._db.add(event)
        return event

    async def record_events_from_diff(
        self,
        project_id: UUID,
        branch: str,
        old_graph: Graph | None,
        new_graph: Graph,
        user_id: str,
        user_name: str | None,
        commit_hash: str | None,
        *,
        origin_resolver: OriginResolver | None = None,
        default_origin: str = ChangeOrigin.HUMAN,
        occurred_at: datetime | None = None,
        auto_commit: bool = True,
        skips: ChangeEventSkips | None = None,
    ) -> list[EntityChangeEvent]:
        """Compare two RDFLib graphs and generate change events."""
        old_graph = old_graph if old_graph is not None else Graph()
        old_entities = _get_declared_entities(old_graph)
        new_entities = _get_declared_entities(new_graph)
        skipped = ChangeEventSkips() if skips is None else skips

        def fits_column(iri: str) -> bool:
            if len(iri) <= MAX_ENTITY_IRI_LENGTH:
                return True
            skipped.overlong_iris += 1
            return False

        if occurred_at is not None:
            occurred_at = _clamp_occurrence_time(occurred_at)
            # Inline saves and reconciliation hold the same branch lock. The
            # query also sees earlier commits already flushed in this transaction.
            latest = await self._db.scalar(
                select(EntityChangeEvent.created_at)
                .where(
                    EntityChangeEvent.project_id == project_id, EntityChangeEvent.branch == branch
                )
                .order_by(EntityChangeEvent.created_at.desc())
                .limit(1)
            )
            if latest is not None:
                if latest.tzinfo is None:
                    latest = latest.replace(tzinfo=UTC)
                occurred_at = max(occurred_at, latest + timedelta(microseconds=1))

        events: list[EntityChangeEvent] = []

        async def append(event: EntityChangeEvent) -> None:
            if occurred_at is not None:
                event.created_at = occurred_at
            events.append(event)
            if len(events) % _EVENT_INSERT_BATCH_SIZE == 0:
                await self._db.flush()

        old_iris = set(old_entities.keys())
        new_iris = set(new_entities.keys())

        created_iris = {iri for iri in new_iris - old_iris if fits_column(iri)}
        origins: dict[str, dict[str, str | None]] = {}
        if origin_resolver is not None and created_iris:
            created = {
                iri: {
                    "labels": _literal_values(new_graph, URIRef(iri), RDFS.label),
                    "definitions": _literal_values(new_graph, URIRef(iri), SKOS.definition),
                }
                for iri in created_iris
            }
            try:
                # Roll back a failed ledger query without poisoning the event transaction.
                async with self._db.begin_nested():
                    origins = await origin_resolver(project_id, created)
            except Exception:
                logger.warning("Failed to resolve change origins", exc_info=True)
                origins = {}
            del created

        # Created entities
        for iri in created_iris:
            provenance = origins.get(iri, {})
            batch_id = provenance.get("batch_id")
            await append(
                await self.record_event(
                    project_id,
                    branch,
                    iri,
                    new_entities[iri],
                    ChangeEventType.CREATE,
                    user_id,
                    user_name,
                    commit_hash,
                    new_values=dict(_entity_values(new_graph, URIRef(iri), new_entities[iri])),
                    origin=provenance.get("origin") or default_origin,
                    origin_model=provenance.get("model"),
                    origin_batch_id=UUID(batch_id) if batch_id is not None else None,
                )
            )

        # Deleted entities
        for iri in old_iris - new_iris:
            if not fits_column(iri):
                continue
            await append(
                await self.record_event(
                    project_id,
                    branch,
                    iri,
                    old_entities[iri],
                    ChangeEventType.DELETE,
                    user_id,
                    user_name,
                    commit_hash,
                    old_values=dict(_entity_values(old_graph, URIRef(iri), old_entities[iri])),
                    origin=default_origin,
                )
            )

        # Modified entities
        for iri in old_iris & new_iris:
            uri = URIRef(iri)
            etype = new_entities[iri]
            old_values = _entity_values(old_graph, uri, etype)
            new_values = _entity_values(new_graph, uri, etype)
            changed_fields = [
                field for field in old_values if old_values[field] != new_values[field]
            ]
            old_vals: dict[str, object] = {field: old_values[field] for field in changed_fields}
            new_vals: dict[str, object] = {field: new_values[field] for field in changed_fields}

            if not changed_fields and _entity_statements_changed(old_graph, new_graph, uri):
                changed_fields.append("other")

            if not changed_fields or not fits_column(iri):
                continue

            # Determine event type
            if "labels" in changed_fields and len(changed_fields) == 1:
                event_type = ChangeEventType.RENAME
            elif "parents" in changed_fields and len(changed_fields) == 1:
                event_type = ChangeEventType.REPARENT
            elif "deprecated" in changed_fields:
                event_type = ChangeEventType.DEPRECATE
            else:
                event_type = ChangeEventType.UPDATE

            await append(
                await self.record_event(
                    project_id,
                    branch,
                    iri,
                    etype,
                    event_type,
                    user_id,
                    user_name,
                    commit_hash,
                    changed_fields,
                    old_vals or None,
                    new_vals or None,
                    origin=default_origin,
                )
            )

        if len(events) % _EVENT_INSERT_BATCH_SIZE:
            await self._db.flush()
        if events and auto_commit:
            await self._db.commit()
        if skips is None:
            skipped.warn()

        return events

    async def list_changes(
        self,
        project_id: UUID,
        *,
        branch: str,
        days: int = 7,
        origin: Sequence[str] | None = None,
        author: list[str] | None = None,
        entity_type: list[str] | None = None,
        event_type: list[str] | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> ProjectChangesResponse:
        until = datetime.now(UTC)
        position = None
        if cursor is not None:
            until, timestamp, iri = _read_cursor(cursor, days, until)
            position = timestamp, iri
        since = until - timedelta(days=days)
        filters = _event_filters(
            project_id,
            branch,
            since,
            until,
            origin=origin,
            author=author,
            entity_type=entity_type,
            event_type=event_type,
        )
        entities = _group_changes(filters)
        page = select(entities).order_by(entities.c.last_changed_at.desc(), entities.c.entity_iri)
        if position is not None:
            timestamp, iri = position
            page = page.where(
                or_(
                    entities.c.last_changed_at < timestamp,
                    (entities.c.last_changed_at == timestamp) & (entities.c.entity_iri > iri),
                )
            )
        rows = (await self._db.execute(page.limit(limit + 1))).all()
        next_cursor = None
        if len(rows) > limit:
            rows = rows[:limit]
            last = rows[-1]
            key = f"{_utc_iso(until)}|{_utc_iso(last.last_changed_at)}|{last.entity_iri}"
            next_cursor = urlsafe_b64encode(key.encode()).decode()

        items = []
        if rows:
            # Only fetch event payloads for entities on this page. Older events within the
            # window still contribute to an item's net changes even if its timeline is capped.
            event = EntityChangeEvent
            query = (
                select(event)
                .where(*filters, event.entity_iri.in_([row.entity_iri for row in rows]))
                .order_by(event.created_at.desc(), event.id.desc())
            )
            events_by_iri: dict[str, list[EntityChangeEvent]] = defaultdict(list)
            for result in (await self._db.execute(query)).scalars():
                events_by_iri[result.entity_iri].append(result)
            items = [
                _project_change_item(row.entity_iri, row.net_change, events_by_iri[row.entity_iri])
                for row in rows
            ]

        return ProjectChangesResponse(
            window=ChangesWindow(
                days=days, since=_utc_iso(since), until=_utc_iso(until), branch=branch
            ),
            summary=await self._changes_summary(entities, filters),
            items=items,
            next_cursor=next_cursor,
        )

    async def _changes_summary(
        self, entities: CTE, filters: list[ColumnElement[bool]]
    ) -> ChangesSummary:
        event = EntityChangeEvent
        net_counts = select(entities.c.net_change, func.count()).group_by(entities.c.net_change)
        by_net_change: dict[NetChange, int] = {
            "added": 0,
            "modified": 0,
            "removed": 0,
            "transient": 0,
        }
        for net_change, count in await self._db.execute(net_counts):
            by_net_change[net_change] = count

        origin_counts = select(event.origin, func.count()).where(*filters).group_by(event.origin)
        by_origin = dict.fromkeys(ChangeOrigin, 0)
        for origin, count in await self._db.execute(origin_counts):
            by_origin[ChangeOrigin(origin)] = count

        # Count by stable user ID and take the most recent known display name.
        authors = (
            select(
                event.user_id,
                event.user_name,
                func.count().over(partition_by=event.user_id).label("event_count"),
                func.row_number()
                .over(
                    partition_by=event.user_id,
                    order_by=(event.user_name.is_(None), event.created_at.desc(), event.id.desc()),
                )
                .label("author_rank"),
            )
            .where(*filters)
            .subquery()
        ).c
        author_counts = (
            select(authors.user_id, authors.user_name, authors.event_count)
            .where(authors.author_rank == 1)
            .order_by(authors.event_count.desc(), authors.user_id)
        )
        return ChangesSummary(
            total_events=sum(by_origin.values()),
            entities_changed=sum(by_net_change.values()),
            by_net_change=by_net_change,
            by_origin=by_origin,
            authors=[
                ChangeAuthorCount(
                    user_id=row.user_id, user_name=row.user_name, event_count=row.event_count
                )
                for row in await self._db.execute(author_counts)
            ],
        )

    async def list_changed_entities(
        self,
        project_id: UUID,
        *,
        branch: str,
        days: int = 7,
        origin: Sequence[str] | None = None,
    ) -> ChangedEntitiesResponse:
        until = datetime.now(UTC)
        filters = _event_filters(
            project_id, branch, until - timedelta(days=days), until, origin=origin
        )
        entities = _group_changes(filters)
        page = (
            select(entities)
            .order_by(entities.c.last_changed_at.desc(), entities.c.entity_iri)
            .limit(_MARKER_LIMIT + 1)
        )
        rows = (await self._db.execute(page)).all()
        if not rows:
            return ChangedEntitiesResponse(items=[], truncated=False)

        shown = rows[:_MARKER_LIMIT]
        event = EntityChangeEvent
        origins_query = (
            select(event.entity_iri, event.origin)
            .where(*filters, event.entity_iri.in_([row.entity_iri for row in shown]))
            .distinct()
        )
        origins: dict[str, list[ChangeOrigin]] = defaultdict(list)
        for iri, event_origin in await self._db.execute(origins_query):
            origins[iri].append(ChangeOrigin(event_origin))

        return ChangedEntitiesResponse(
            items=[
                ChangedEntity(
                    entity_iri=row.entity_iri,
                    net_change=row.net_change,
                    origins=sorted(origins[row.entity_iri]),
                    last_changed_at=_utc_iso(row.last_changed_at),
                )
                for row in shown
            ],
            truncated=len(rows) > _MARKER_LIMIT,
        )

    async def get_entity_history(
        self,
        project_id: UUID,
        entity_iri: str,
        branch: str | None = None,
        limit: int = 50,
    ) -> EntityHistoryResponse:
        query = (
            select(EntityChangeEvent)
            .where(
                EntityChangeEvent.project_id == project_id,
                EntityChangeEvent.entity_iri == entity_iri,
            )
            .order_by(EntityChangeEvent.created_at.desc())
            .limit(limit)
        )
        if branch:
            query = query.where(EntityChangeEvent.branch == branch)

        result = await self._db.execute(query)
        rows = result.scalars().all()

        events = [_serialize_event(row) for row in rows]

        # Get total count
        count_q = (
            select(func.count())
            .select_from(EntityChangeEvent)
            .where(
                EntityChangeEvent.project_id == project_id,
                EntityChangeEvent.entity_iri == entity_iri,
            )
        )
        if branch:
            count_q = count_q.where(EntityChangeEvent.branch == branch)
        total = (await self._db.execute(count_q)).scalar() or 0

        return EntityHistoryResponse(
            entity_iri=entity_iri,
            events=events,
            total=total,
        )

    async def get_activity(self, project_id: UUID, days: int = 30) -> ProjectActivity:

        cutoff = datetime.now(UTC) - timedelta(days=days)

        # Daily counts
        daily_q = (
            select(
                func.date_trunc("day", EntityChangeEvent.created_at).label("day"),
                func.count().label("cnt"),
            )
            .where(
                EntityChangeEvent.project_id == project_id,
                EntityChangeEvent.created_at >= cutoff,
            )
            .group_by("day")
            .order_by("day")
        )
        daily_result = await self._db.execute(daily_q)
        daily_counts = [
            ActivityDay(date=row.day.strftime("%Y-%m-%d"), count=row.cnt) for row in daily_result
        ]

        # Total
        total_q = (
            select(func.count())
            .select_from(EntityChangeEvent)
            .where(
                EntityChangeEvent.project_id == project_id,
                EntityChangeEvent.created_at >= cutoff,
            )
        )
        total = (await self._db.execute(total_q)).scalar() or 0

        # Top editors
        editors_q = (
            select(
                EntityChangeEvent.user_id,
                EntityChangeEvent.user_name,
                func.count().label("cnt"),
            )
            .where(
                EntityChangeEvent.project_id == project_id,
                EntityChangeEvent.created_at >= cutoff,
            )
            .group_by(EntityChangeEvent.user_id, EntityChangeEvent.user_name)
            .order_by(func.count().desc())
            .limit(10)
        )
        editors_result = await self._db.execute(editors_q)
        top_editors = [
            TopEditor(user_id=row.user_id, user_name=row.user_name or "", edit_count=row.cnt)
            for row in editors_result
        ]

        return ProjectActivity(
            daily_counts=daily_counts,
            total_events=total,
            top_editors=top_editors,
        )

    async def get_hot_entities(self, project_id: UUID, limit: int = 20) -> list[HotEntity]:

        cutoff = datetime.now(UTC) - timedelta(days=30)

        q = (
            select(
                EntityChangeEvent.entity_iri,
                EntityChangeEvent.entity_type,
                func.count().label("edit_count"),
                func.count(func.distinct(EntityChangeEvent.user_id)).label("editor_count"),
                func.max(EntityChangeEvent.created_at).label("last_edited_at"),
            )
            .where(
                EntityChangeEvent.project_id == project_id,
                EntityChangeEvent.created_at >= cutoff,
            )
            .group_by(EntityChangeEvent.entity_iri, EntityChangeEvent.entity_type)
            .order_by(func.count().desc())
            .limit(limit)
        )
        result = await self._db.execute(q)

        return [
            HotEntity(
                entity_iri=row.entity_iri,
                entity_type=row.entity_type,
                label=None,  # Could resolve from graph, but keeping it simple
                edit_count=row.edit_count,
                editor_count=row.editor_count,
                last_edited_at=row.last_edited_at.isoformat(),
            )
            for row in result
        ]

    async def get_contributors(self, project_id: UUID, days: int = 30) -> list[ContributorStats]:

        cutoff = datetime.now(UTC) - timedelta(days=days)

        q = (
            select(
                EntityChangeEvent.user_id,
                EntityChangeEvent.user_name,
                func.count().filter(EntityChangeEvent.event_type == "create").label("create_count"),
                func.count()
                .filter(
                    EntityChangeEvent.event_type.in_(["update", "rename", "reparent", "deprecate"])
                )
                .label("update_count"),
                func.count().filter(EntityChangeEvent.event_type == "delete").label("delete_count"),
                func.count().label("total_count"),
                func.max(EntityChangeEvent.created_at).label("last_active_at"),
            )
            .where(
                EntityChangeEvent.project_id == project_id,
                EntityChangeEvent.created_at >= cutoff,
            )
            .group_by(EntityChangeEvent.user_id, EntityChangeEvent.user_name)
            .order_by(func.count().desc())
        )
        result = await self._db.execute(q)

        return [
            ContributorStats(
                user_id=row.user_id,
                user_name=row.user_name or "",
                create_count=row.create_count,
                update_count=row.update_count,
                delete_count=row.delete_count,
                total_count=row.total_count,
                last_active_at=row.last_active_at.isoformat(),
            )
            for row in result
        ]
