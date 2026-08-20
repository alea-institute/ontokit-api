"""Warm-index/cold-RDF annotation classification parity."""

from __future__ import annotations

import uuid

import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.models.project import Project
from ontokit.services.ontology import OntologyService
from ontokit.services.ontology_index import OntologyIndexService


def _annotation_values(detail: object) -> set[tuple[str, str]]:
    annotations = detail["annotations"] if isinstance(detail, dict) else detail.annotations
    values: set[tuple[str, str]] = set()
    for annotation in annotations:
        property_iri = (
            annotation["property_iri"]
            if isinstance(annotation, dict)
            else str(annotation.property_iri)
        )
        annotation_values = (
            annotation["values"] if isinstance(annotation, dict) else annotation.values
        )
        for value in annotation_values:
            text = value["value"] if isinstance(value, dict) else value.value
            values.add((property_iri, text))
    return values


@pytest.mark.asyncio
async def test_declared_annotation_predicates_match_in_warm_and_cold_paths(
    real_db_session: AsyncSession,
) -> None:
    """Both paths include built-ins/declarations and reject data/object/unknown predicates."""
    project_id = uuid.uuid4()
    branch = "annotation-parity"
    class_iri = URIRef("https://example.org/Thing")
    custom_annotation = URIRef("https://example.org/customAnnotation")
    undeclared = URIRef("https://example.org/undeclared")
    object_property = URIRef("https://example.org/objectProperty")
    data_property = URIRef("https://example.org/dataProperty")

    graph = Graph()
    graph.add((class_iri, RDF.type, OWL.Class))
    graph.add((class_iri, RDFS.label, Literal("Thing", lang="en")))
    graph.add((class_iri, RDFS.comment, Literal("Dedicated comment", lang="en")))
    graph.add((class_iri, SKOS.related, URIRef("https://example.org/Related")))
    graph.add((class_iri, SKOS.broader, URIRef("https://example.org/Broader")))
    graph.add((class_iri, SKOS.narrower, URIRef("https://example.org/Narrower")))
    graph.add((custom_annotation, RDF.type, OWL.AnnotationProperty))
    graph.add((class_iri, custom_annotation, Literal("Custom value")))
    graph.add((class_iri, undeclared, Literal("Must stay hidden")))
    graph.add((object_property, RDF.type, OWL.ObjectProperty))
    graph.add((class_iri, object_property, URIRef("https://example.org/Object")))
    graph.add((data_property, RDF.type, OWL.DatatypeProperty))
    graph.add((class_iri, data_property, Literal("Must also stay hidden")))

    real_db_session.add(Project(id=project_id, name="Annotation parity", owner_id="test"))
    await real_db_session.commit()
    try:
        cold = await OntologyService()._class_to_response(graph, class_iri)
        index = OntologyIndexService(real_db_session)
        await index.full_reindex(project_id, branch, graph, "a" * 40)
        warm = await index.get_class_detail(project_id, branch, str(class_iri))

        assert warm is not None
        expected = {
            (str(SKOS.related), "https://example.org/Related"),
            (str(SKOS.broader), "https://example.org/Broader"),
            (str(SKOS.narrower), "https://example.org/Narrower"),
            (str(custom_annotation), "Custom value"),
        }
        assert _annotation_values(cold) == expected
        assert _annotation_values(warm) == expected
        assert warm["labels"] == [{"value": "Thing", "lang": "en"}]
        assert warm["comments"] == [{"value": "Dedicated comment", "lang": "en"}]
    finally:
        await real_db_session.execute(delete(Project).where(Project.id == project_id))
        await real_db_session.commit()
