"""Entity discovery parity for bare ``rdf:Property`` and ``rdfs:Class`` (CatholicOS#121).

Covers both search paths:

- the RDFLib (cold) path in ``OntologyService``;
- the index (warm) path in ``OntologyIndexService``, at the indexing and
  result-mapping layers (the real-database round trip lives in
  ``tests/integration/test_annotation_classification_parity.py``).
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from rdflib import Graph, Literal, Namespace
from rdflib.namespace import OWL, RDF, RDFS

from ontokit.models.ontology_index import IndexedEntity
from ontokit.schemas.owl_class import EntitySearchResponse
from ontokit.services.ontology import OntologyService
from ontokit.services.ontology_index import (
    ENTITY_TYPE_OBJECT_PROPERTY,
    ENTITY_TYPE_RDF_PROPERTY,
    OntologyIndexService,
)

EX = Namespace("https://example.org/parity#")
PROJECT_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
BRANCH = "main"


def ae1_graph() -> Graph:
    """AE1 fixture: a bare rdf:Property and a dual-typed rdf:Property/owl:ObjectProperty."""
    g = Graph()
    g.add((EX.hasFoo, RDF.type, RDF.Property))
    g.add((EX.hasBar, RDF.type, RDF.Property))
    g.add((EX.hasBar, RDF.type, OWL.ObjectProperty))
    return g


def rdfs_class_graph() -> Graph:
    """A mixed hierarchy: an rdfs:Class-only root with owl and rdfs children."""
    g = Graph()
    g.add((EX.Agent, RDF.type, RDFS.Class))
    g.add((EX.Agent, RDFS.label, Literal("Agent", lang="en")))
    g.add((EX.Person, RDF.type, OWL.Class))
    g.add((EX.Person, RDFS.subClassOf, EX.Agent))
    g.add((EX.Robot, RDF.type, RDFS.Class))
    g.add((EX.Robot, RDFS.subClassOf, EX.Agent))
    # Dual-typed class must be counted and listed once.
    g.add((EX.Place, RDF.type, OWL.Class))
    g.add((EX.Place, RDF.type, RDFS.Class))
    return g


def _service(graph: Graph) -> OntologyService:
    svc = OntologyService(storage=None)
    svc.set_graph(PROJECT_ID, BRANCH, graph)
    return svc


# ---------------------------------------------------------------------------
# RDFLib (cold) path
# ---------------------------------------------------------------------------


class TestRdflibSearchBareRdfProperty:
    @pytest.mark.asyncio
    async def test_ae1_bare_property_found_with_null_kind(self) -> None:
        result = await _service(ae1_graph()).search_entities(PROJECT_ID, "has")
        by_iri = {r.iri: r for r in result.results}

        assert set(by_iri) == {str(EX.hasFoo), str(EX.hasBar)}
        assert by_iri[str(EX.hasFoo)].entity_type == "property"
        assert by_iri[str(EX.hasFoo)].property_kind is None

    @pytest.mark.asyncio
    async def test_ae1_dual_typed_property_appears_once_with_owl_kind(self) -> None:
        result = await _service(ae1_graph()).search_entities(PROJECT_ID, "has")
        bars = [r for r in result.results if r.iri == str(EX.hasBar)]

        assert len(bars) == 1
        assert bars[0].entity_type == "property"
        assert bars[0].property_kind == "object"
        assert result.total == 2

    @pytest.mark.asyncio
    async def test_bare_property_included_in_property_filter(self) -> None:
        result = await _service(ae1_graph()).search_entities(
            PROJECT_ID, "hasFoo", entity_types=["property"]
        )
        assert [r.iri for r in result.results] == [str(EX.hasFoo)]

    @pytest.mark.asyncio
    async def test_bare_property_excluded_from_class_filter(self) -> None:
        result = await _service(ae1_graph()).search_entities(
            PROJECT_ID, "has", entity_types=["class"]
        )
        assert result.results == []

    @pytest.mark.asyncio
    async def test_property_kind_null_in_schema_response(self) -> None:
        """The serialized API response carries ``property_kind: null``."""
        result = await _service(ae1_graph()).search_entities(PROJECT_ID, "hasFoo")
        payload = EntitySearchResponse.model_validate(result.model_dump()).model_dump(mode="json")

        assert payload["results"][0]["iri"] == str(EX.hasFoo)
        assert "property_kind" in payload["results"][0]
        assert payload["results"][0]["property_kind"] is None


class TestRdflibSearchRdfsClass:
    @pytest.mark.asyncio
    async def test_rdfs_class_only_entity_is_searchable_as_class(self) -> None:
        result = await _service(rdfs_class_graph()).search_entities(
            PROJECT_ID, "Robot", entity_types=["class"]
        )
        assert [(r.iri, r.entity_type) for r in result.results] == [(str(EX.Robot), "class")]

    @pytest.mark.asyncio
    async def test_dual_typed_class_appears_once(self) -> None:
        result = await _service(rdfs_class_graph()).search_entities(PROJECT_ID, "Place")
        assert [r.iri for r in result.results] == [str(EX.Place)]


class TestRdflibGetClassResolvesRdfsClass:
    @pytest.mark.asyncio
    async def test_get_class_resolves_rdfs_class_only_subject(self) -> None:
        """A bare rdfs:Class listed by tree/search must also resolve via get_class."""
        cls = await _service(rdfs_class_graph()).get_class(PROJECT_ID, str(EX.Robot), branch=BRANCH)
        assert cls is not None
        assert str(cls.iri) == str(EX.Robot)

    @pytest.mark.asyncio
    async def test_get_class_rdfs_class_root_children_counted(self) -> None:
        cls = await _service(rdfs_class_graph()).get_class(PROJECT_ID, str(EX.Agent), branch=BRANCH)
        assert cls is not None
        assert cls.child_count == 2

    @pytest.mark.asyncio
    async def test_get_class_still_rejects_non_class(self) -> None:
        cls = await _service(ae1_graph()).get_class(PROJECT_ID, str(EX.hasFoo), branch=BRANCH)
        assert cls is None


class TestRdflibClassTreeIncludesRdfsClass:
    @pytest.mark.asyncio
    async def test_rdfs_class_root_listed(self) -> None:
        roots = await _service(rdfs_class_graph()).get_root_classes(PROJECT_ID, branch=BRANCH)
        assert sorted(str(c.iri) for c in roots) == sorted([str(EX.Agent), str(EX.Place)])

    @pytest.mark.asyncio
    async def test_rdfs_class_children_listed(self) -> None:
        children = await _service(rdfs_class_graph()).get_class_children(
            PROJECT_ID, str(EX.Agent), branch=BRANCH
        )
        assert sorted(str(c.iri) for c in children) == sorted([str(EX.Person), str(EX.Robot)])

    @pytest.mark.asyncio
    async def test_rdfs_class_root_child_count(self) -> None:
        nodes = await _service(rdfs_class_graph()).get_root_tree_nodes(PROJECT_ID, branch=BRANCH)
        by_iri = {n.iri: n for n in nodes}
        assert by_iri[str(EX.Agent)].child_count == 2

    @pytest.mark.asyncio
    async def test_class_count_includes_rdfs_class_once(self) -> None:
        count = await _service(rdfs_class_graph()).get_class_count(PROJECT_ID, BRANCH)
        assert count == 4  # Agent, Person, Robot, Place (dual-typed counted once)

    @pytest.mark.asyncio
    async def test_ancestor_path_through_rdfs_class(self) -> None:
        path = await _service(rdfs_class_graph()).get_ancestor_path(
            PROJECT_ID, str(EX.Robot), branch=BRANCH
        )
        assert [n.iri for n in path] == [str(EX.Agent)]


# ---------------------------------------------------------------------------
# Index (warm) path
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_db() -> AsyncMock:
    session = AsyncMock()
    session.execute = AsyncMock()
    session.add = Mock()
    return session


async def _indexed_entity_types(graph: Graph, mock_db: AsyncMock) -> dict[str, str]:
    """Run ``_index_graph`` and return ``{iri: entity_type}`` for inserted entity rows."""
    service = OntologyIndexService(db=mock_db)
    inserted: dict[str, str] = {}

    async def capture(model: type[Any], rows: list[dict[str, Any]]) -> None:
        if model is IndexedEntity:
            for row in rows:
                assert row["iri"] not in inserted, f"{row['iri']} indexed twice"
                inserted[row["iri"]] = row["entity_type"]

    service._batch_insert = capture  # type: ignore[method-assign]
    await service._index_graph(PROJECT_ID, BRANCH, graph)
    return inserted


class TestIndexStoresRdfProperty:
    @pytest.mark.asyncio
    async def test_bare_property_indexed_as_rdf_property(self, mock_db: AsyncMock) -> None:
        types = await _indexed_entity_types(ae1_graph(), mock_db)
        assert types[str(EX.hasFoo)] == ENTITY_TYPE_RDF_PROPERTY

    @pytest.mark.asyncio
    async def test_dual_typed_property_indexed_once_with_owl_type(self, mock_db: AsyncMock) -> None:
        types = await _indexed_entity_types(ae1_graph(), mock_db)
        assert types[str(EX.hasBar)] == ENTITY_TYPE_OBJECT_PROPERTY


def _search_rows(*rows: tuple[str, str]) -> list[MagicMock]:
    count = MagicMock()
    count.scalar.return_value = len(rows)
    entity_rows = []
    for iri, entity_type in rows:
        row = MagicMock()
        row.id = f"id-{iri}"
        row.iri = iri
        row.local_name = iri.rsplit("#", 1)[-1]
        row.entity_type = entity_type
        row.deprecated = False
        entity_rows.append(row)
    entities = MagicMock()
    entities.all.return_value = entity_rows
    labels = MagicMock()
    labels.scalars.return_value.all.return_value = []
    return [count, entities, labels]


class TestIndexSearchRdfProperty:
    @pytest.mark.asyncio
    async def test_rdf_property_reported_as_property_with_null_kind(
        self, mock_db: AsyncMock
    ) -> None:
        mock_db.execute.side_effect = _search_rows(
            (str(EX.hasBar), ENTITY_TYPE_OBJECT_PROPERTY),
            (str(EX.hasFoo), ENTITY_TYPE_RDF_PROPERTY),
        )
        result = await OntologyIndexService(db=mock_db).search_entities(PROJECT_ID, BRANCH, "has")
        by_iri = {r["iri"]: r for r in result["results"]}

        assert by_iri[str(EX.hasFoo)]["entity_type"] == "property"
        assert by_iri[str(EX.hasFoo)]["property_kind"] is None
        assert by_iri[str(EX.hasBar)]["entity_type"] == "property"
        assert by_iri[str(EX.hasBar)]["property_kind"] == "object"
        # The response validates against the public schema.
        EntitySearchResponse.model_validate(result)

    @pytest.mark.asyncio
    async def test_property_filter_includes_rdf_property_type(self, mock_db: AsyncMock) -> None:
        mock_db.execute.side_effect = _search_rows()
        await OntologyIndexService(db=mock_db).search_entities(
            PROJECT_ID, BRANCH, "has", entity_types=["property"]
        )
        count_stmt = mock_db.execute.call_args_list[0].args[0]
        compiled = count_stmt.compile(compile_kwargs={"literal_binds": True})
        assert f"'{ENTITY_TYPE_RDF_PROPERTY}'" in str(compiled)
