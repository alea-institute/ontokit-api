"""Compact RDF annotations for machine-translated literals.

Each translation is represented by one eight-triple ``owl:Axiom`` block. The vocabulary
uses ``dcterms:created`` for its standard date term and the stable
``https://ontokit.dev/ns/translation#`` namespace for the three OntoKit-specific terms
``method``, ``state``, and ``recordDigest``. The digest is the normalized SHA-256 hash of
the associated ``translation_records.id`` and can therefore be resolved without exposing
the database identifier directly in a published ontology.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF
from rdflib.term import Node

from ontokit.models.translation import hash_literal_value

if TYPE_CHECKING:
    from ontokit.models.translation import TranslationRecord

DCTERMS = Namespace("http://purl.org/dc/terms/")
ONTOKIT_TRANSLATION = Namespace("https://ontokit.dev/ns/translation#")


@dataclass(frozen=True, slots=True)
class TranslationAnnotation:
    """The compact provenance values stored on a translated literal's axiom."""

    method: str
    state: str
    created: datetime
    record_digest: str


def translation_record_digest(record: TranslationRecord) -> str:
    """Return the stable, non-reversible digest used to link an annotation to its row."""
    if record.id is None:
        raise ValueError("translation record must have an id before it can be annotated")
    return hash_literal_value(str(record.id))


def annotate(
    graph: Graph,
    subject: Node,
    predicate: Node,
    literal: Literal,
    meta: TranslationAnnotation,
) -> None:
    """Add or replace the single provenance axiom for an exact translated literal."""
    remove_annotation(graph, subject, predicate, literal)
    axiom = _axiom_identifier(subject, predicate, literal)
    graph.add((axiom, RDF.type, OWL.Axiom))
    graph.add((axiom, OWL.annotatedSource, subject))
    graph.add((axiom, OWL.annotatedProperty, predicate))
    graph.add((axiom, OWL.annotatedTarget, literal))
    graph.add((axiom, ONTOKIT_TRANSLATION.method, Literal(meta.method)))
    graph.add((axiom, ONTOKIT_TRANSLATION.state, Literal(meta.state)))
    graph.add((axiom, DCTERMS.created, Literal(meta.created)))
    graph.add((axiom, ONTOKIT_TRANSLATION.recordDigest, Literal(meta.record_digest)))


def read_annotation(
    graph: Graph,
    subject: Node,
    predicate: Node,
    literal: Literal,
) -> TranslationAnnotation | None:
    """Read provenance for an exact literal, or return ``None`` for human-authored text."""
    axioms = _matching_axioms(graph, subject, predicate, literal)
    if not axioms:
        return None

    axiom = axioms[0]
    method = graph.value(axiom, ONTOKIT_TRANSLATION.method)
    state = graph.value(axiom, ONTOKIT_TRANSLATION.state)
    created = graph.value(axiom, DCTERMS.created)
    digest = graph.value(axiom, ONTOKIT_TRANSLATION.recordDigest)
    if not isinstance(method, Literal):
        return None
    if not isinstance(state, Literal):
        return None
    if not isinstance(created, Literal):
        return None
    if not isinstance(digest, Literal):
        return None

    created_value = created.toPython()
    if not isinstance(created_value, datetime):
        return None
    return TranslationAnnotation(
        method=str(method),
        state=str(state),
        created=created_value,
        record_digest=str(digest),
    )


def remove_annotation(
    graph: Graph,
    subject: Node,
    predicate: Node,
    literal: Literal,
) -> None:
    """Remove all matching axiom blocks without changing the annotated label triple."""
    for axiom in _matching_axioms(graph, subject, predicate, literal):
        graph.remove((axiom, None, None))


def _matching_axioms(
    graph: Graph,
    subject: Node,
    predicate: Node,
    literal: Literal,
) -> list[Node]:
    return [
        axiom
        for axiom in graph.subjects(OWL.annotatedSource, subject)
        if (axiom, RDF.type, OWL.Axiom) in graph
        and (axiom, OWL.annotatedProperty, predicate) in graph
        and (axiom, OWL.annotatedTarget, literal) in graph
    ]


def _axiom_identifier(subject: Node, predicate: Node, literal: Literal) -> URIRef:
    identity = "\u001f".join((subject.n3(), predicate.n3(), literal.n3()))
    return ONTOKIT_TRANSLATION[f"axiom-{hash_literal_value(identity)}"]


__all__ = [
    "DCTERMS",
    "ONTOKIT_TRANSLATION",
    "TranslationAnnotation",
    "annotate",
    "read_annotation",
    "remove_annotation",
    "translation_record_digest",
]
