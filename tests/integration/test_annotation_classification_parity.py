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


def _annotation_values_with_lang(detail: object, property_iri: str) -> set[tuple[str, str]]:
    """Return ``{(value, lang)}`` for one annotation property on either path."""
    annotations = detail["annotations"] if isinstance(detail, dict) else detail.annotations
    values: set[tuple[str, str]] = set()
    for annotation in annotations:
        iri = (
            annotation["property_iri"]
            if isinstance(annotation, dict)
            else str(annotation.property_iri)
        )
        if iri != property_iri:
            continue
        annotation_values = (
            annotation["values"] if isinstance(annotation, dict) else annotation.values
        )
        for value in annotation_values:
            if isinstance(value, dict):
                values.add((value["value"], value["lang"]))
            else:
                values.add((value.value, value.lang))
    return values


@pytest.mark.asyncio
async def test_alt_label_values_and_language_tags_match_in_warm_and_cold_paths(
    real_db_session: AsyncSession,
) -> None:
    """skos:altLabel values keep the same text and language tags on both paths (#212)."""
    project_id = uuid.uuid4()
    branch = "altlabel-parity"
    class_iri = URIRef("https://example.org/Car")

    graph = Graph()
    graph.add((class_iri, RDF.type, OWL.Class))
    graph.add((class_iri, RDFS.label, Literal("Car", lang="en")))
    graph.add((class_iri, SKOS.altLabel, Literal("Auto", lang="de")))
    graph.add((class_iri, SKOS.altLabel, Literal("Motorcar", lang="en-gb")))
    graph.add((class_iri, SKOS.altLabel, Literal("Automobile")))

    real_db_session.add(Project(id=project_id, name="AltLabel parity", owner_id="test"))
    await real_db_session.commit()
    try:
        cold = await OntologyService()._class_to_response(graph, class_iri)
        index = OntologyIndexService(real_db_session)
        await index.full_reindex(project_id, branch, graph, "b" * 40)
        warm = await index.get_class_detail(project_id, branch, str(class_iri))

        assert warm is not None
        cold_values = _annotation_values_with_lang(cold, str(SKOS.altLabel))
        warm_values = _annotation_values_with_lang(warm, str(SKOS.altLabel))
        assert cold_values == {("Auto", "de"), ("Motorcar", "en-gb"), ("Automobile", "")}
        assert warm_values == cold_values
    finally:
        await real_db_session.execute(delete(Project).where(Project.id == project_id))
        await real_db_session.commit()


@pytest.mark.asyncio
async def test_entity_discovery_matches_in_warm_and_cold_paths(
    real_db_session: AsyncSession,
) -> None:
    """Bare rdf:Property and rdfs:Class are discovered identically on both paths (#121)."""
    ex = "https://example.org/disc#"
    has_foo = URIRef(f"{ex}hasFoo")
    has_bar = URIRef(f"{ex}hasBar")
    agent = URIRef(f"{ex}Agent")
    person = URIRef(f"{ex}Person")
    robot = URIRef(f"{ex}Robot")
    project_id = uuid.uuid4()
    branch = "discovery-parity"

    graph = Graph()
    # AE1: bare rdf:Property, and a dual-typed rdf:Property + owl:ObjectProperty.
    graph.add((has_foo, RDF.type, RDF.Property))
    graph.add((has_bar, RDF.type, RDF.Property))
    graph.add((has_bar, RDF.type, OWL.ObjectProperty))
    # rdfs:Class-only root with an owl:Class and an rdfs:Class child.
    graph.add((agent, RDF.type, RDFS.Class))
    graph.add((person, RDF.type, OWL.Class))
    graph.add((person, RDFS.subClassOf, agent))
    graph.add((robot, RDF.type, RDFS.Class))
    graph.add((robot, RDFS.subClassOf, agent))

    cold_service = OntologyService()
    cold_service.set_graph(project_id, branch, graph)

    real_db_session.add(Project(id=project_id, name="Discovery parity", owner_id="test"))
    await real_db_session.commit()
    try:
        index = OntologyIndexService(real_db_session)
        await index.full_reindex(project_id, branch, graph, "c" * 40)

        for query in ("has", "*"):
            cold = await cold_service.search_entities(project_id, query, branch=branch)
            warm = await index.search_entities(project_id, branch, query)
            cold_set = {(r.iri, r.entity_type, r.property_kind) for r in cold.results}
            warm_set = {(r["iri"], r["entity_type"], r["property_kind"]) for r in warm["results"]}
            assert warm_set == cold_set, query
            assert warm["total"] == cold.total, query

        has_results = await cold_service.search_entities(project_id, "has", branch=branch)
        assert {(r.iri, r.property_kind) for r in has_results.results} == {
            (str(has_foo), None),
            (str(has_bar), "object"),
        }

        cold_roots = await cold_service.get_root_tree_nodes(project_id, branch=branch)
        warm_roots = await index.get_root_classes(project_id, branch)
        assert {(n.iri, n.child_count) for n in cold_roots} == {
            (n["iri"], n["child_count"]) for n in warm_roots
        }
        assert {n.iri for n in cold_roots} == {str(agent)}

        cold_children = await cold_service.get_children_tree_nodes(
            project_id, str(agent), branch=branch
        )
        warm_children = await index.get_class_children(project_id, branch, str(agent))
        assert {n.iri for n in cold_children} == {n["iri"] for n in warm_children}
        assert {n.iri for n in cold_children} == {str(person), str(robot)}

        # A bare rdfs:Class resolves on the cold get_class and warm detail paths.
        cold_robot = await cold_service.get_class(project_id, str(robot), branch=branch)
        warm_robot = await index.get_class_detail(project_id, branch, str(robot))
        assert cold_robot is not None
        assert warm_robot is not None
        assert str(cold_robot.iri) == warm_robot["iri"] == str(robot)
    finally:
        await real_db_session.execute(delete(Project).where(Project.id == project_id))
        await real_db_session.commit()
