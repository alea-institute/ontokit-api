"""Tests for compact in-graph translation provenance annotations."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from rdflib import Graph, Literal, Namespace
from rdflib.namespace import OWL, RDF, SKOS
from rdflib.term import Node

from ontokit.models.translation import TranslationRecord, hash_literal_value
from ontokit.services.translation_annotations import (
    DCTERMS,
    ONTOKIT_TRANSLATION,
    TranslationAnnotation,
    annotate,
    read_annotation,
    remove_annotation,
    translation_record_digest,
)

EX = Namespace("https://example.org/")
CREATED = datetime(2026, 8, 9, 15, 30, tzinfo=UTC)


def _meta(*, state: str = "provisional", method: str = "consensus") -> TranslationAnnotation:
    return TranslationAnnotation(
        method=method,
        state=state,
        created=CREATED,
        record_digest=hash_literal_value("translation-record-123"),
    )


def _axioms(graph: Graph) -> set[Node]:
    return set(graph.subjects(RDF.type, OWL.Axiom))


def test_annotation_round_trips_as_one_compact_axiom_block() -> None:
    graph = Graph()
    literal = Literal("Catedral", lang="es")
    graph.add((EX.cathedral, SKOS.prefLabel, literal))

    annotate(graph, EX.cathedral, SKOS.prefLabel, literal, _meta())

    axiom = _axioms(graph).pop()
    assert len(set(graph.triples((axiom, None, None)))) == 8
    assert set(graph.objects(axiom, ONTOKIT_TRANSLATION.method)) == {Literal("consensus")}
    assert set(graph.objects(axiom, ONTOKIT_TRANSLATION.state)) == {Literal("provisional")}
    assert len(set(graph.objects(axiom, DCTERMS.created))) == 1
    assert set(graph.objects(axiom, ONTOKIT_TRANSLATION.recordDigest)) == {
        Literal(hash_literal_value("translation-record-123"))
    }

    restored = Graph().parse(data=graph.serialize(format="turtle"), format="turtle")
    assert len(_axioms(restored)) == 1
    assert read_annotation(restored, EX.cathedral, SKOS.prefLabel, literal) == _meta()


def test_promoting_annotation_updates_state_in_place() -> None:
    graph = Graph()
    literal = Literal("Catedral", lang="es")
    graph.add((EX.cathedral, SKOS.prefLabel, literal))
    annotate(graph, EX.cathedral, SKOS.prefLabel, literal, _meta())
    original_axiom = _axioms(graph).pop()

    annotate(graph, EX.cathedral, SKOS.prefLabel, literal, _meta(state="verified"))

    assert _axioms(graph) == {original_axiom}
    assert read_annotation(graph, EX.cathedral, SKOS.prefLabel, literal) == _meta(state="verified")


def test_remove_annotation_leaves_no_orphan_axiom_triples() -> None:
    graph = Graph()
    literal = Literal("Catedral", lang="es")
    graph.add((EX.cathedral, SKOS.prefLabel, literal))
    annotate(graph, EX.cathedral, SKOS.prefLabel, literal, _meta())
    axiom = _axioms(graph).pop()

    remove_annotation(graph, EX.cathedral, SKOS.prefLabel, literal)

    assert not _axioms(graph)
    assert not list(graph.triples((axiom, None, None)))
    assert (EX.cathedral, SKOS.prefLabel, literal) in graph


def test_human_authored_label_is_untouched() -> None:
    graph = Graph()
    literal = Literal("Cathedral", lang="en")
    graph.add((EX.cathedral, SKOS.prefLabel, literal))

    assert read_annotation(graph, EX.cathedral, SKOS.prefLabel, literal) is None
    remove_annotation(graph, EX.cathedral, SKOS.prefLabel, literal)

    assert set(graph) == {(EX.cathedral, SKOS.prefLabel, literal)}


def test_reannotating_same_literal_keeps_one_axiom_block() -> None:
    graph = Graph()
    literal = Literal("Catedral", lang="es")
    graph.add((EX.cathedral, SKOS.prefLabel, literal))

    annotate(graph, EX.cathedral, SKOS.prefLabel, literal, _meta())
    annotate(graph, EX.cathedral, SKOS.prefLabel, literal, _meta(method="confidence"))

    assert len(_axioms(graph)) == 1
    assert read_annotation(graph, EX.cathedral, SKOS.prefLabel, literal) == _meta(
        method="confidence"
    )


def test_literals_in_different_languages_have_independent_blocks() -> None:
    graph = Graph()
    spanish = Literal("Catedral", lang="es")
    french = Literal("Cathédrale", lang="fr")
    graph.add((EX.cathedral, SKOS.prefLabel, spanish))
    graph.add((EX.cathedral, SKOS.prefLabel, french))

    annotate(graph, EX.cathedral, SKOS.prefLabel, spanish, _meta())
    annotate(graph, EX.cathedral, SKOS.prefLabel, french, _meta(method="confidence"))

    assert len(_axioms(graph)) == 2
    assert read_annotation(graph, EX.cathedral, SKOS.prefLabel, spanish) == _meta()
    assert read_annotation(graph, EX.cathedral, SKOS.prefLabel, french) == _meta(
        method="confidence"
    )


def test_translation_record_digest_is_stable_for_record_identity() -> None:
    record = TranslationRecord(
        id=uuid.UUID("12345678-1234-5678-1234-567812345678"),
        project_id=uuid.uuid4(),
        entity_iri=str(EX.cathedral),
        predicate=str(SKOS.prefLabel),
        language="es",
        source_value_hash=hash_literal_value("Cathedral"),
        translated_value_hash=hash_literal_value("Catedral"),
        model_name="model",
        model_version="1",
        method="consensus",
        score=0.95,
    )

    assert translation_record_digest(record) == hash_literal_value(str(record.id))
