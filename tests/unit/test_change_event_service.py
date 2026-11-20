"""Tests for ChangeEventService (ontokit/services/change_event_service.py)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS

from ontokit.models.change_event import ChangeEventType, EntityChangeEvent
from ontokit.services.change_event_service import ChangeEventService

PROJECT_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
BRANCH = "main"
USER_ID = "user-123"
USER_NAME = "Test User"
COMMIT_HASH = "a1b2c3d4"
ENTITY_IRI = "http://example.org/ontology#Person"


@pytest.fixture
def mock_db() -> AsyncMock:
    """Create an async mock of AsyncSession."""
    session = AsyncMock()
    session.commit = AsyncMock()
    session.execute = AsyncMock()
    session.add = Mock()
    return session


@pytest.fixture
def service(mock_db: AsyncMock) -> ChangeEventService:
    """Create a ChangeEventService with mocked DB."""
    return ChangeEventService(mock_db)


class TestRecordEvent:
    """Tests for record_event()."""

    @pytest.mark.asyncio
    async def test_creates_entity_change_event(
        self, service: ChangeEventService, mock_db: AsyncMock
    ) -> None:
        """record_event creates an EntityChangeEvent and adds it to the session."""
        result = await service.record_event(
            project_id=PROJECT_ID,
            branch=BRANCH,
            entity_iri=ENTITY_IRI,
            entity_type="class",
            event_type=ChangeEventType.CREATE,
            user_id=USER_ID,
            user_name=USER_NAME,
            commit_hash=COMMIT_HASH,
        )
        mock_db.add.assert_called_once()
        assert isinstance(result, EntityChangeEvent)
        assert result.project_id == PROJECT_ID
        assert result.entity_iri == ENTITY_IRI
        assert result.event_type == ChangeEventType.CREATE
        assert result.origin == "human"

    @pytest.mark.asyncio
    async def test_defaults_changed_fields_to_empty_list(
        self,
        service: ChangeEventService,
        mock_db: AsyncMock,  # noqa: ARG002
    ) -> None:
        """changed_fields defaults to an empty list when None."""
        result = await service.record_event(
            project_id=PROJECT_ID,
            branch=BRANCH,
            entity_iri=ENTITY_IRI,
            entity_type="class",
            event_type=ChangeEventType.UPDATE,
            user_id=USER_ID,
            changed_fields=None,
        )
        assert result.changed_fields == []


class TestRecordEventsFromDiff:
    """Tests for record_events_from_diff()."""

    @pytest.mark.asyncio
    async def test_detects_created_entities(
        self, service: ChangeEventService, mock_db: AsyncMock
    ) -> None:
        """Entities in new_graph but not old_graph produce CREATE events."""
        old_graph = Graph()
        new_graph = Graph()
        uri = URIRef("http://example.org/ontology#NewClass")
        new_graph.add((uri, RDF.type, OWL.Class))
        new_graph.add((uri, RDFS.label, Literal("New Class")))
        new_graph.add((uri, SKOS.definition, Literal("A new concept.")))
        parent = URIRef("https://example.org/ontology#Parent")
        new_graph.add((uri, RDFS.subClassOf, parent))

        events = await service.record_events_from_diff(
            PROJECT_ID, BRANCH, old_graph, new_graph, USER_ID, USER_NAME, COMMIT_HASH
        )
        assert len(events) == 1
        assert events[0].event_type == ChangeEventType.CREATE
        assert events[0].new_values is not None
        assert events[0].new_values["labels"] == ["New Class"]
        assert events[0].new_values["definitions"] == ["A new concept."]
        assert events[0].new_values["parents"] == [str(parent)]
        mock_db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_detects_deleted_entities(
        self,
        service: ChangeEventService,
        mock_db: AsyncMock,  # noqa: ARG002
    ) -> None:
        """Entities in old_graph but not new_graph produce DELETE events."""
        old_graph = Graph()
        uri = URIRef("http://example.org/ontology#OldClass")
        old_graph.add((uri, RDF.type, OWL.Class))
        old_graph.add((uri, RDFS.label, Literal("Old Class")))
        old_graph.add((uri, SKOS.definition, Literal("An old concept.")))
        parent = URIRef("https://example.org/ontology#Parent")
        old_graph.add((uri, RDFS.subClassOf, parent))

        new_graph = Graph()

        events = await service.record_events_from_diff(
            PROJECT_ID, BRANCH, old_graph, new_graph, USER_ID, USER_NAME, COMMIT_HASH
        )
        assert len(events) == 1
        assert events[0].event_type == ChangeEventType.DELETE
        assert events[0].old_values is not None
        assert events[0].old_values["labels"] == ["Old Class"]
        assert events[0].old_values["definitions"] == ["An old concept."]
        assert events[0].old_values["parents"] == [str(parent)]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("before,after", [(None, "New"), ("Old", "New"), ("Old", None)])
    async def test_captures_definition_changes(
        self, service: ChangeEventService, before: str | None, after: str | None
    ) -> None:
        uri = URIRef(ENTITY_IRI)
        old_graph, new_graph = Graph(), Graph()
        for graph, definition in ((old_graph, before), (new_graph, after)):
            graph.add((uri, RDF.type, OWL.Class))
            if definition is not None:
                graph.add((uri, SKOS.definition, Literal(definition)))
            graph.add((uri, SKOS.definition, URIRef("https://example.org/ignored")))

        (event,) = await service.record_events_from_diff(
            PROJECT_ID, BRANCH, old_graph, new_graph, USER_ID, USER_NAME, COMMIT_HASH
        )
        assert event.changed_fields == ["definitions"]
        assert event.old_values == {"definitions": [before] if before else []}
        assert event.new_values == {"definitions": [after] if after else []}

    @pytest.mark.asyncio
    async def test_captures_comment_values_on_system_updates(
        self, service: ChangeEventService
    ) -> None:
        uri = URIRef(ENTITY_IRI)
        old_graph, new_graph = Graph(), Graph()
        for graph, comment in ((old_graph, "Old comment"), (new_graph, "New comment")):
            graph.add((uri, RDF.type, OWL.Class))
            graph.add((uri, RDFS.comment, Literal(comment)))

        (event,) = await service.record_events_from_diff(
            PROJECT_ID,
            BRANCH,
            old_graph,
            new_graph,
            USER_ID,
            USER_NAME,
            COMMIT_HASH,
            default_origin="system",
        )
        assert event.changed_fields == ["comments"]
        assert event.old_values == {"comments": ["Old comment"]}
        assert event.new_values == {"comments": ["New comment"]}
        assert event.origin == "system"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("resolver_raises", [False, True])
    async def test_resolves_all_creates_together(
        self, service: ChangeEventService, mock_db: AsyncMock, resolver_raises: bool
    ) -> None:
        graph = Graph()
        input_values = {}
        for label in ("Generated", "Edited", "Manual"):
            iri = f"https://example.org/ontology#{label}"
            uri = URIRef(iri)
            graph.add((uri, RDF.type, OWL.Class))
            graph.add((uri, RDFS.label, Literal(label)))
            graph.add((uri, SKOS.definition, Literal(f"{label} definition")))
            input_values[iri] = {"labels": [label], "definitions": [f"{label} definition"]}
        generated, edited, manual = input_values
        batch = uuid.uuid4()
        resolver = AsyncMock(
            return_value={
                generated: {"origin": "ai", "model": "test-model", "batch_id": str(batch)},
                edited: {"origin": "ai_edited", "model": "test-model", "batch_id": str(batch)},
            },
            side_effect=RuntimeError("resolver unavailable") if resolver_raises else None,
        )
        mock_db.begin_nested = MagicMock()

        events = await service.record_events_from_diff(
            PROJECT_ID,
            BRANCH,
            None,
            graph,
            USER_ID,
            USER_NAME,
            COMMIT_HASH,
            origin_resolver=resolver,
        )

        resolver.assert_awaited_once_with(PROJECT_ID, input_values)
        mock_db.commit.assert_awaited_once()
        by_iri = {event.entity_iri: event for event in events}
        assert by_iri.keys() == input_values.keys()
        for iri, origin in ((generated, "ai"), (edited, "ai_edited"), (manual, "human")):
            event = by_iri[iri]
            if resolver_raises or origin == "human":
                assert (event.origin, event.origin_model, event.origin_batch_id) == (
                    "human",
                    None,
                    None,
                )
            else:
                assert (event.origin, event.origin_model, event.origin_batch_id) == (
                    origin,
                    "test-model",
                    batch,
                )

    @pytest.mark.asyncio
    async def test_detects_renamed_entities(
        self,
        service: ChangeEventService,
        mock_db: AsyncMock,  # noqa: ARG002
    ) -> None:
        """Entities with only label changes produce RENAME events."""
        uri = URIRef("http://example.org/ontology#MyClass")

        old_graph = Graph()
        old_graph.add((uri, RDF.type, OWL.Class))
        old_graph.add((uri, RDFS.label, Literal("Old Name")))

        new_graph = Graph()
        new_graph.add((uri, RDF.type, OWL.Class))
        new_graph.add((uri, RDFS.label, Literal("New Name")))

        events = await service.record_events_from_diff(
            PROJECT_ID, BRANCH, old_graph, new_graph, USER_ID, USER_NAME, COMMIT_HASH
        )
        assert len(events) == 1
        assert events[0].event_type == ChangeEventType.RENAME

    @pytest.mark.asyncio
    async def test_no_changes_produces_no_events(
        self, service: ChangeEventService, mock_db: AsyncMock
    ) -> None:
        """Identical graphs produce no events and no commit."""
        graph = Graph()
        uri = URIRef("http://example.org/ontology#Same")
        graph.add((uri, RDF.type, OWL.Class))
        graph.add((uri, RDFS.label, Literal("Same")))

        events = await service.record_events_from_diff(
            PROJECT_ID, BRANCH, graph, graph, USER_ID, USER_NAME, COMMIT_HASH
        )
        assert events == []
        mock_db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_none_old_graph_all_creates(
        self,
        service: ChangeEventService,
        mock_db: AsyncMock,  # noqa: ARG002
    ) -> None:
        """When old_graph is None, all entities in new_graph are CREATE events."""
        new_graph = Graph()
        uri1 = URIRef("http://example.org/ontology#A")
        uri2 = URIRef("http://example.org/ontology#B")
        new_graph.add((uri1, RDF.type, OWL.Class))
        new_graph.add((uri2, RDF.type, OWL.Class))

        events = await service.record_events_from_diff(
            PROJECT_ID, BRANCH, None, new_graph, USER_ID, USER_NAME, COMMIT_HASH
        )
        assert len(events) == 2
        assert all(e.event_type == ChangeEventType.CREATE for e in events)


class TestGetEntityHistory:
    """Tests for get_entity_history()."""

    @pytest.mark.asyncio
    async def test_returns_entity_history_response(
        self, service: ChangeEventService, mock_db: AsyncMock
    ) -> None:
        """get_entity_history returns an EntityHistoryResponse with events."""
        row = MagicMock()
        row.id = uuid.uuid4()
        row.project_id = PROJECT_ID
        row.branch = BRANCH
        row.entity_iri = ENTITY_IRI
        row.entity_type = "class"
        row.event_type = "create"
        row.user_id = USER_ID
        row.user_name = USER_NAME
        row.commit_hash = COMMIT_HASH
        row.origin = "ai"
        row.origin_model = "test-model"
        row.origin_batch_id = uuid.uuid4()
        row.changed_fields = []
        row.old_values = None
        row.new_values = None
        row.created_at = datetime.now(UTC)

        # First execute: items query
        items_result = MagicMock()
        items_result.scalars.return_value.all.return_value = [row]

        # Second execute: count query
        count_result = MagicMock()
        count_result.scalar.return_value = 1

        mock_db.execute.side_effect = [items_result, count_result]

        response = await service.get_entity_history(PROJECT_ID, ENTITY_IRI)
        assert response.entity_iri == ENTITY_IRI
        assert response.total == 1
        assert len(response.events) == 1
        (event,) = response.events
        assert event.origin == "ai"
        assert event.origin_model == "test-model"
        assert event.origin_batch_id == str(row.origin_batch_id)


class TestGetActivity:
    """Tests for get_activity()."""

    @pytest.mark.asyncio
    async def test_returns_project_activity(
        self, service: ChangeEventService, mock_db: AsyncMock
    ) -> None:
        """get_activity returns ProjectActivity with daily_counts, total, and top_editors."""
        day_row = MagicMock()
        day_row.day = datetime(2025, 1, 15, tzinfo=UTC)
        day_row.cnt = 5

        daily_result = MagicMock()
        daily_result.__iter__ = Mock(return_value=iter([day_row]))

        total_result = MagicMock()
        total_result.scalar.return_value = 5

        editor_row = MagicMock()
        editor_row.user_id = USER_ID
        editor_row.user_name = USER_NAME
        editor_row.cnt = 5

        editors_result = MagicMock()
        editors_result.__iter__ = Mock(return_value=iter([editor_row]))

        mock_db.execute.side_effect = [daily_result, total_result, editors_result]

        activity = await service.get_activity(PROJECT_ID, days=30)
        assert activity.total_events == 5
        assert len(activity.daily_counts) == 1
        assert activity.daily_counts[0].count == 5
        assert len(activity.top_editors) == 1


class TestGetHotEntities:
    """Tests for get_hot_entities()."""

    @pytest.mark.asyncio
    async def test_returns_hot_entities(
        self, service: ChangeEventService, mock_db: AsyncMock
    ) -> None:
        """get_hot_entities returns a list of HotEntity objects."""
        row = MagicMock()
        row.entity_iri = ENTITY_IRI
        row.entity_type = "class"
        row.edit_count = 10
        row.editor_count = 3
        row.last_edited_at = datetime.now(UTC)

        result = MagicMock()
        result.__iter__ = Mock(return_value=iter([row]))
        mock_db.execute.return_value = result

        hot = await service.get_hot_entities(PROJECT_ID, limit=20)
        assert len(hot) == 1
        assert hot[0].entity_iri == ENTITY_IRI
        assert hot[0].edit_count == 10
        assert hot[0].editor_count == 3


class TestGetContributors:
    """Tests for get_contributors()."""

    @pytest.mark.asyncio
    async def test_returns_contributor_stats(
        self, service: ChangeEventService, mock_db: AsyncMock
    ) -> None:
        """get_contributors returns a list of ContributorStats objects."""
        row = MagicMock()
        row.user_id = USER_ID
        row.user_name = USER_NAME
        row.create_count = 3
        row.update_count = 5
        row.delete_count = 1
        row.total_count = 9
        row.last_active_at = datetime.now(UTC)

        result = MagicMock()
        result.__iter__ = Mock(return_value=iter([row]))
        mock_db.execute.return_value = result

        contributors = await service.get_contributors(PROJECT_ID, days=30)
        assert len(contributors) == 1
        assert contributors[0].user_id == USER_ID
        assert contributors[0].total_count == 9
        assert contributors[0].create_count == 3
