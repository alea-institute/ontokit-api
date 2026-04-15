"""Tests for duplicate detection service (ontokit/services/duplicate_detection_service.py)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from rdflib import Graph, Literal, Namespace
from rdflib.namespace import OWL, RDF, RDFS

from ontokit.services.duplicate_detection_service import find_duplicates, find_duplicates_sql

EX = Namespace("http://example.org/")
PROJECT_ID = UUID("12345678-1234-5678-1234-567812345678")


def _row(**kwargs: Any) -> SimpleNamespace:
    """Create a fake DB row with named attributes."""
    return SimpleNamespace(**kwargs)


def _mock_db(
    entities: list[SimpleNamespace],
    similar_map: dict[str, list[SimpleNamespace]] | None = None,
) -> AsyncMock:
    """Build a mock AsyncSession that returns entity rows then per-label similar rows."""
    db = AsyncMock()
    call_count = 0
    if similar_map is None:
        similar_map = {}

    async def fake_execute(stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:  # noqa: ARG001
        nonlocal call_count
        call_count += 1

        # First call: SET LOCAL (no result needed)
        if call_count == 1:
            return MagicMock()

        # Second call: entity labels query
        if call_count == 2:
            result = MagicMock()
            result.fetchall.return_value = entities
            return result

        # Subsequent calls: per-entity similar labels lookup
        if params and "label" in params:
            label = params["label"]
            matches = similar_map.get(label, [])
            result = MagicMock()
            # Make result iterable (for `for match in similar:`)
            result.__iter__ = lambda self: iter(matches)  # noqa: ARG005
            return result

        result = MagicMock()
        result.__iter__ = lambda self: iter([])  # noqa: ARG005
        return result

    db.execute = fake_execute
    return db


# ---------------------------------------------------------------------------
# SQL-based tests
# ---------------------------------------------------------------------------


class TestFindDuplicatesSql:
    """Tests for find_duplicates_sql (PostgreSQL trigram approach)."""

    @pytest.mark.asyncio
    async def test_finds_duplicates(self) -> None:
        """Entities with similar labels are clustered."""
        e1_id = UUID("aaaaaaaa-0000-0000-0000-000000000001")
        e2_id = UUID("aaaaaaaa-0000-0000-0000-000000000002")

        entities = [
            _row(entity_id=e1_id, iri="http://ex.org/Foo", entity_type="class", value="Widget"),
            _row(entity_id=e2_id, iri="http://ex.org/Bar", entity_type="class", value="Widget"),
        ]

        similar_map = {
            "Widget": [
                _row(entity_id=e2_id, iri="http://ex.org/Bar", value="Widget", sim=1.0),
            ],
        }

        db = _mock_db(entities, similar_map)
        result = await find_duplicates_sql(db, PROJECT_ID, "main", threshold=0.85)

        assert len(result.clusters) == 1
        iris = {e.iri for e in result.clusters[0].entities}
        assert "http://ex.org/Foo" in iris
        assert "http://ex.org/Bar" in iris
        assert result.clusters[0].similarity == 1.0

    @pytest.mark.asyncio
    async def test_no_duplicates(self) -> None:
        """Entities with different labels produce no clusters."""
        e1_id = UUID("aaaaaaaa-0000-0000-0000-000000000001")
        e2_id = UUID("aaaaaaaa-0000-0000-0000-000000000002")

        entities = [
            _row(entity_id=e1_id, iri="http://ex.org/Apple", entity_type="class", value="Apple"),
            _row(entity_id=e2_id, iri="http://ex.org/Banana", entity_type="class", value="Banana"),
        ]

        db = _mock_db(entities)  # No similar_map → no matches
        result = await find_duplicates_sql(db, PROJECT_ID, "main", threshold=0.85)

        assert len(result.clusters) == 0

    @pytest.mark.asyncio
    async def test_empty_project(self) -> None:
        """Empty project produces no clusters."""
        db = _mock_db([])
        result = await find_duplicates_sql(db, PROJECT_ID, "main")
        assert len(result.clusters) == 0
        assert result.threshold == 0.85

    @pytest.mark.asyncio
    async def test_skips_already_matched(self) -> None:
        """Entities already matched are skipped in subsequent lookups."""
        e1_id = UUID("aaaaaaaa-0000-0000-0000-000000000001")
        e2_id = UUID("aaaaaaaa-0000-0000-0000-000000000002")
        e3_id = UUID("aaaaaaaa-0000-0000-0000-000000000003")

        entities = [
            _row(entity_id=e1_id, iri="http://ex.org/A", entity_type="class", value="Thing"),
            _row(entity_id=e2_id, iri="http://ex.org/B", entity_type="class", value="Thing"),
            _row(entity_id=e3_id, iri="http://ex.org/C", entity_type="class", value="Other"),
        ]

        similar_map = {
            "Thing": [
                _row(entity_id=e2_id, iri="http://ex.org/B", value="Thing", sim=1.0),
            ],
            # e2 ("Thing") would also match e1, but e2 is already matched → skipped
            # e3 ("Other") has no matches
        }

        db = _mock_db(entities, similar_map)
        result = await find_duplicates_sql(db, PROJECT_ID, "main")

        assert len(result.clusters) == 1
        assert len(result.clusters[0].entities) == 2

    @pytest.mark.asyncio
    async def test_threshold_passed(self) -> None:
        """Custom threshold is passed through and completes without error."""
        db = _mock_db([])
        result = await find_duplicates_sql(db, PROJECT_ID, "main", threshold=0.95)
        assert result.threshold == 0.95

    @pytest.mark.asyncio
    async def test_reverse_pair_similarity(self) -> None:
        """Cluster similarity uses reverse pair lookup when needed."""
        e1_id = UUID("aaaaaaaa-0000-0000-0000-000000000001")
        e2_id = UUID("aaaaaaaa-0000-0000-0000-000000000002")
        e3_id = UUID("aaaaaaaa-0000-0000-0000-000000000003")

        entities = [
            _row(entity_id=e1_id, iri="http://ex.org/A", entity_type="class", value="Widget A"),
            _row(entity_id=e2_id, iri="http://ex.org/B", entity_type="class", value="Widget B"),
            _row(entity_id=e3_id, iri="http://ex.org/C", entity_type="class", value="Widget C"),
        ]

        similar_map = {
            "Widget A": [
                _row(entity_id=e2_id, iri="http://ex.org/B", value="Widget B", sim=0.9),
                _row(entity_id=e3_id, iri="http://ex.org/C", value="Widget C", sim=0.88),
            ],
        }

        db = _mock_db(entities, similar_map)
        result = await find_duplicates_sql(db, PROJECT_ID, "main", threshold=0.85)

        assert len(result.clusters) == 1
        assert len(result.clusters[0].entities) == 3
        assert result.clusters[0].similarity >= 0.85

    @pytest.mark.asyncio
    async def test_transitive_clustering(self) -> None:
        """A~B and B~C should cluster all three even if A!~C directly."""
        e1_id = UUID("aaaaaaaa-0000-0000-0000-000000000001")
        e2_id = UUID("aaaaaaaa-0000-0000-0000-000000000002")
        e3_id = UUID("aaaaaaaa-0000-0000-0000-000000000003")

        entities = [
            _row(entity_id=e1_id, iri="http://ex.org/A", entity_type="class", value="Alpha"),
            _row(entity_id=e2_id, iri="http://ex.org/B", entity_type="class", value="Alpho"),
            _row(entity_id=e3_id, iri="http://ex.org/C", entity_type="class", value="Alphi"),
        ]

        # A matches B, B matches C, but A does NOT directly match C
        similar_map = {
            "Alpha": [
                _row(entity_id=e2_id, iri="http://ex.org/B", value="Alpho", sim=0.9),
            ],
            "Alpho": [
                _row(entity_id=e3_id, iri="http://ex.org/C", value="Alphi", sim=0.88),
            ],
            # "Alphi" → no matches (A is too different)
        }

        db = _mock_db(entities, similar_map)
        result = await find_duplicates_sql(db, PROJECT_ID, "main", threshold=0.85)

        assert len(result.clusters) == 1
        iris = {e.iri for e in result.clusters[0].entities}
        assert iris == {"http://ex.org/A", "http://ex.org/B", "http://ex.org/C"}


def test_find_duplicates_by_label() -> None:
    g = Graph()
    g.add((EX.Foo, RDF.type, OWL.Class))
    g.add((EX.Foo, RDFS.label, Literal("Widget")))
    g.add((EX.Bar, RDF.type, OWL.Class))
    g.add((EX.Bar, RDFS.label, Literal("Widget")))

    result = find_duplicates(g, threshold=0.85)
    assert len(result.clusters) == 1
    iris = {e.iri for e in result.clusters[0].entities}
    assert str(EX.Foo) in iris
    assert str(EX.Bar) in iris


def test_no_duplicates() -> None:
    g = Graph()
    g.add((EX.Apple, RDF.type, OWL.Class))
    g.add((EX.Apple, RDFS.label, Literal("Apple")))
    g.add((EX.Banana, RDF.type, OWL.Class))
    g.add((EX.Banana, RDFS.label, Literal("Banana")))

    result = find_duplicates(g, threshold=0.85)
    assert len(result.clusters) == 0


def test_similarity_zero_not_falsy() -> None:
    """Similarity of 0.0 should be handled correctly (not treated as None/falsy)."""
    g = Graph()
    g.add((EX.A, RDF.type, OWL.Class))
    g.add((EX.A, RDFS.label, Literal("AAAA")))
    g.add((EX.B, RDF.type, OWL.Class))
    g.add((EX.B, RDFS.label, Literal("ZZZZ")))

    result = find_duplicates(g, threshold=0.85)
    # Very different labels should not cluster
    assert len(result.clusters) == 0


def test_find_duplicates_cross_type() -> None:
    """Duplicates are only detected within the same entity type."""
    g = Graph()
    g.add((EX.Widget, RDF.type, OWL.Class))
    g.add((EX.Widget, RDFS.label, Literal("Widget")))
    g.add((EX.widgetProp, RDF.type, OWL.ObjectProperty))
    g.add((EX.widgetProp, RDFS.label, Literal("Widget")))

    result = find_duplicates(g, threshold=0.85)
    # Same label but different types — should NOT cluster
    assert len(result.clusters) == 0


def test_find_duplicates_similar_labels() -> None:
    """Very similar (but not identical) labels should cluster above threshold."""
    g = Graph()
    g.add((EX.PersonInfo, RDF.type, OWL.Class))
    g.add((EX.PersonInfo, RDFS.label, Literal("Person Information")))
    g.add((EX.PersonInformation, RDF.type, OWL.Class))
    g.add((EX.PersonInformation, RDFS.label, Literal("Person Informations")))

    result = find_duplicates(g, threshold=0.85)
    assert len(result.clusters) == 1
    assert result.clusters[0].similarity >= 0.85
