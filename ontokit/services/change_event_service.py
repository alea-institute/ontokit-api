"""Change event service — track entity modifications from graph diffs."""

import logging
from base64 import b64decode, urlsafe_b64encode
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import chain
from typing import Any, Literal, cast
from uuid import UUID

from rdflib import BNode, Graph, URIRef
from rdflib import Literal as RDFLiteral
from rdflib.compare import isomorphic
from rdflib.namespace import OWL, RDF, RDFS, SKOS
from rdflib.term import Node
from sqlalchemy import Boolean, case, func, literal, or_, select
from sqlalchemy.exc import CompileError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql import ColumnElement
from sqlalchemy.sql.compiler import SQLCompiler
from sqlalchemy.sql.functions import FunctionElement
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
_LITERAL_FIELDS: dict[Node, str] = {
    RDFS.label: "labels",
    SKOS.definition: "definitions",
    RDFS.comment: "comments",
}
_Triple = tuple[Node, Node, Node]
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


class _json_document(FunctionElement[Any]):
    """A JSON column in the form the JSON tests below read: JSONB on PostgreSQL."""

    inherit_cache = True
    name = "json_document"


@compiles(_json_document)
def _json_document_as_is(element: _json_document, compiler: SQLCompiler, **kw: Any) -> str:
    (document,) = element.clauses
    return compiler.process(document, **kw)


@compiles(_json_document, "postgresql")
def _json_document_postgresql(element: _json_document, compiler: SQLCompiler, **kw: Any) -> str:
    (document,) = element.clauses
    return f"CAST({compiler.process(document, **kw)} AS JSONB)"


class _json_has(FunctionElement[bool]):
    """True when a JSON array holds the string, or a JSON object has the key."""

    type = Boolean()
    inherit_cache = True
    name = "json_has"


class _json_truthy(FunctionElement[bool]):
    """True when a JSON object's member is present and truthy as Python reads it."""

    type = Boolean()
    inherit_cache = True
    name = "json_truthy"


def _json_arguments(
    element: FunctionElement[bool], compiler: SQLCompiler, **kw: Any
) -> tuple[str, str]:
    document, key = element.clauses
    return compiler.process(document, **kw), compiler.process(key, **kw)


@compiles(_json_has)
@compiles(_json_truthy)
def _json_test_unsupported(element: FunctionElement[bool], compiler: SQLCompiler, **_: Any) -> str:
    raise CompileError(f"{element.name} is not implemented for {compiler.dialect.name}")


@compiles(_json_has, "postgresql")
def _json_has_postgresql(element: _json_has, compiler: SQLCompiler, **kw: Any) -> str:
    document, key = _json_arguments(element, compiler, **kw)
    return f"coalesce(jsonb_exists({document}, {key}), false)"


@compiles(_json_has, "sqlite")
def _json_has_sqlite(element: _json_has, compiler: SQLCompiler, **kw: Any) -> str:
    document, key = _json_arguments(element, compiler, **kw)
    return (
        f"EXISTS (SELECT 1 FROM json_each({document}) WHERE CASE json_type({document}) "
        f"WHEN 'array' THEN json_each.type = 'text' AND json_each.value = {key} "
        f"WHEN 'object' THEN json_each.key = {key} ELSE 0 END)"
    )


@compiles(_json_truthy, "postgresql")
def _json_truthy_postgresql(element: _json_truthy, compiler: SQLCompiler, **kw: Any) -> str:
    document, key = _json_arguments(element, compiler, **kw)
    value = f"({document} -> {key})"
    return (
        f"coalesce(CASE jsonb_typeof({value}) "
        f"WHEN 'array' THEN jsonb_array_length({value}) > 0 "
        f"WHEN 'object' THEN {value} <> '{{}}'::jsonb "
        f"WHEN 'string' THEN {value} <> '\"\"'::jsonb "
        f"WHEN 'number' THEN {value} <> '0'::jsonb "
        f"WHEN 'boolean' THEN {value} = 'true'::jsonb "
        f"ELSE false END, false)"
    )


@compiles(_json_truthy, "sqlite")
def _json_truthy_sqlite(element: _json_truthy, compiler: SQLCompiler, **kw: Any) -> str:
    document, key = _json_arguments(element, compiler, **kw)
    path = f"('$.\"' || {key} || '\"')"
    value = f"json_extract({document}, {path})"
    return (
        f"coalesce(CASE json_type({document}, {path}) "
        f"WHEN 'array' THEN json_array_length({document}, {path}) > 0 "
        f"WHEN 'object' THEN {value} <> '{{}}' "
        f"WHEN 'text' THEN {value} <> '' "
        f"WHEN 'integer' THEN {value} <> 0 "
        f"WHEN 'real' THEN {value} <> 0 "
        f"WHEN 'true' THEN 1 "
        f"ELSE 0 END, 0)"
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


def _field_changes(events: list[EntityChangeEvent], net_change: NetChange) -> list[FieldChange]:
    """Report the net change per field over the entity's events, newest first.

    A tracked field carries the value before its earliest change and after its
    latest, and is omitted when the two are equal. ``other`` carries no values,
    so it cannot net out: it is reported whenever any event in the window
    changed statements outside the tracked fields, and only for an entity that
    existed at both edges of the window.
    """
    before: dict[ChangeField, ChangeFieldValue] = {}
    after: dict[ChangeField, ChangeFieldValue] = {}
    other_changed = False
    for event in reversed(events):
        old, new = event.old_values or {}, event.new_values or {}
        touched = set(event.changed_fields or [])
        other_changed = other_changed or "other" in touched
        if event.event_type in (ChangeEventType.CREATE, ChangeEventType.DELETE):
            touched.update(old)
            touched.update(new)
        for field in _CHANGE_FIELDS:
            if field not in touched or field == "other":
                continue
            absent: ChangeFieldValue = False if field == "deprecated" else []
            before.setdefault(
                field, old.get(field, absent if event.event_type == "create" else None)
            )
            after[field] = new.get(field, absent if event.event_type == "delete" else None)

    changes = [
        FieldChange(field=field, before=before[field], after=after[field])
        for field in _CHANGE_FIELDS
        if field in before and before[field] != after[field]
    ]
    if other_changed and net_change == "modified":
        changes.append(FieldChange(field="other", before=None, after=None))
    return changes


def _project_change_item(
    entity_iri: str, net_change: NetChange, events: list[EntityChangeEvent], event_count: int
) -> ProjectChangeItem:
    """Build an item from its events, newest first.

    ``events`` may omit window events that cannot change the item: see
    ``_item_events_query`` for the ones it must keep.
    """
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
        event_count=event_count,
        authors=[
            ChangeAuthor(user_id=user_id, user_name=name) for user_id, name in authors.items()
        ],
        field_changes=_field_changes(events, net_change),
        events=[_serialize_event(event) for event in events[:_ITEM_EVENT_LIMIT]],
    )


def _touched(field: ChangeField, events: Any) -> ColumnElement[bool]:
    """Mirror the events ``_field_changes`` reads a field from."""
    key = literal(field)
    listed = _json_has(events.changed_fields, key)
    if field == "other":
        return listed
    edge: ColumnElement[bool] = events.event_type.in_(
        (ChangeEventType.CREATE, ChangeEventType.DELETE)
    )
    return listed | (edge & (_json_has(events.old_values, key) | _json_has(events.new_values, key)))


def _item_events_query(filters: list[ColumnElement[bool]], entity_iris: list[str]) -> Any:
    """Select the window events that decide each entity's item, newest first.

    An item shows its newest events, reads each field's value before its earliest
    change and after its latest, takes its label from the newest event that has
    one, and lists every origin and author with the newest name each author used.
    Every other event only adds to the count, so it stays in the database however
    many times the entity changed.
    """
    event = EntityChangeEvent
    # The window function keeps this subquery separate, so each document is parsed once.
    documents = (
        select(
            event.id,
            event.entity_iri,
            event.event_type,
            event.user_id,
            event.user_name,
            event.origin,
            func.row_number()
            .over(partition_by=event.entity_iri, order_by=(event.created_at, event.id))
            .label("position"),
            *(
                _json_document(column).label(column.key)
                for column in (event.changed_fields, event.old_values, event.new_values)
            ),
        )
        .where(*filters, event.entity_iri.in_(entity_iris))
        .subquery("documents")
    ).c

    def latest(condition: ColumnElement[bool], *partition: Any) -> Any:
        return func.max(case((condition, documents.position))).over(
            partition_by=(documents.entity_iri, *partition)
        )

    def earliest(condition: ColumnElement[bool]) -> Any:
        return func.min(case((condition, documents.position))).over(
            partition_by=documents.entity_iri
        )

    label = literal("labels")
    labelled = _json_truthy(documents.new_values, label) | _json_truthy(documents.old_values, label)
    always = documents.position.isnot(None)
    anchors = [
        latest(always, documents.user_id),
        latest(documents.user_name.isnot(None), documents.user_id),
        latest(always, documents.origin),
        latest(labelled),
    ]
    for field in _CHANGE_FIELDS:
        touched = _touched(field, documents)
        anchors.append(earliest(touched))
        if field != "other":
            anchors.append(latest(touched))
    marked_events = select(
        documents.id,
        documents.position,
        func.count().over(partition_by=documents.entity_iri).label("event_count"),
        *(anchor.label(f"anchor_{index}") for index, anchor in enumerate(anchors)),
    ).subquery("marked")
    marked = marked_events.c
    newest = marked.position > marked.event_count - _ITEM_EVENT_LIMIT
    keep = or_(
        newest,
        *(marked.position == getattr(marked, f"anchor_{index}") for index in range(len(anchors))),
    )
    return (
        select(event, marked.event_count)
        .join(marked_events, marked.id == event.id)
        .where(keep)
        .order_by(event.created_at.desc(), event.id.desc())
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


def _tracked_field(predicate: Node, obj: Node, parent_predicate: URIRef) -> str | None:
    """Name the tracked field whose value a statement supplies, if any."""
    if isinstance(obj, RDFLiteral) and predicate in _LITERAL_FIELDS:
        return _LITERAL_FIELDS[predicate]
    if predicate == parent_predicate and isinstance(obj, URIRef):
        return "parents"
    if predicate == OWL.deprecated:
        return "deprecated"
    return None


def _description(graph: Graph, statements: set[_Triple]) -> Graph:
    """Collect the statements and everything reachable from them through blank nodes."""
    description = Graph()
    pending = list(statements)
    seen: set[BNode] = set()
    while pending:
        triple = pending.pop()
        description.add(triple)
        obj = triple[2]
        if isinstance(obj, BNode) and obj not in seen:
            seen.add(obj)
            pending.extend(graph.triples((obj, None, None)))
    return description


def _other_statements_changed(
    old_graph: Graph, new_graph: Graph, uri: URIRef, entity_type: str, changed_fields: list[str]
) -> bool:
    """Report a change to the entity that its changed tracked fields do not show.

    Statements that supply a changed tracked field are set aside. What remains
    covers every other predicate, anonymous class expressions, and the language
    tags and datatypes of tracked literals whose text stayed the same.
    """
    parent_predicate = RDFS.subPropertyOf if entity_type == "property" else RDFS.subClassOf

    def remaining(graph: Graph) -> set[_Triple]:
        return {
            triple
            for triple in graph.triples((uri, None, None))
            if _tracked_field(triple[1], triple[2], parent_predicate) not in changed_fields
        }

    old_statements, new_statements = remaining(old_graph), remaining(new_graph)
    if any(isinstance(obj, BNode) for _, _, obj in chain(old_statements, new_statements)):
        # Blank-node labels differ between parses of the same content.
        return not isomorphic(
            _description(old_graph, old_statements), _description(new_graph, new_statements)
        )
    return old_statements != new_statements


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

            if _other_statements_changed(old_graph, new_graph, uri, etype, changed_fields):
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
            query = _item_events_query(filters, [row.entity_iri for row in rows])
            events_by_iri: dict[str, list[EntityChangeEvent]] = defaultdict(list)
            counts: dict[str, int] = {}
            for result, event_count in await self._db.execute(query):
                events_by_iri[result.entity_iri].append(result)
                counts[result.entity_iri] = event_count
            items = [
                _project_change_item(
                    row.entity_iri,
                    row.net_change,
                    events_by_iri[row.entity_iri],
                    counts[row.entity_iri],
                )
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
