"""Cross-reference service — find all entities that reference a target IRI."""

from rdflib import Graph, URIRef
from rdflib import Literal as RDFLiteral
from rdflib.namespace import OWL, RDF, RDFS

from ontokit.schemas.quality import (
    CONTEXT_LABELS,
    CrossReference,
    CrossReferenceGroup,
    CrossReferencesResponse,
    ReferenceContext,
)

# Map predicates to reference contexts
_PREDICATE_CONTEXT: dict[URIRef, ReferenceContext] = {
    RDFS.subClassOf: "parent_iris",
    RDFS.domain: "domain_iris",
    RDFS.range: "range_iris",
    RDF.type: "type_iris",
    OWL.equivalentClass: "equivalent_iris",
    OWL.disjointWith: "disjoint_iris",
    OWL.someValuesFrom: "some_values_from",
    RDFS.seeAlso: "see_also",
    OWL.inverseOf: "inverse_of",
}

# Entity type detection in order of precedence
_TYPE_CHECKS: list[tuple[URIRef, str]] = [
    (OWL.Class, "class"),
    (OWL.ObjectProperty, "property"),
    (OWL.DatatypeProperty, "property"),
    (OWL.AnnotationProperty, "property"),
    (OWL.NamedIndividual, "individual"),
]


def _resolve_entity_type(graph: Graph, subject: URIRef) -> str:
    """Determine the entity type of a subject."""
    for rdf_type, label in _TYPE_CHECKS:
        if (subject, RDF.type, rdf_type) in graph:
            return label
    return "unknown"


def _resolve_label(graph: Graph, subject: URIRef) -> str | None:
    """Get first rdfs:label for a subject."""
    for obj in graph.objects(subject, RDFS.label):
        if isinstance(obj, RDFLiteral):
            return str(obj)
    return None


def get_cross_references(graph: Graph, target_iri: str) -> CrossReferencesResponse:
    """Find all entities that reference the target IRI and group by context."""
    target = URIRef(target_iri)

    # Collect references grouped by context
    refs_by_context: dict[ReferenceContext, list[CrossReference]] = {}

    for s, p, _o in graph.triples((None, None, target)):
        if not isinstance(s, URIRef):
            continue

        # Determine context from predicate
        context = _PREDICATE_CONTEXT.get(p)
        if context is None:
            # Check if it's an annotation property with a URI value
            if isinstance(p, URIRef):
                context = "annotation_value"
            else:
                continue

        ref = CrossReference(
            source_iri=str(s),
            source_type=_resolve_entity_type(graph, s),
            source_label=_resolve_label(graph, s),
            reference_context=context,
        )

        refs_by_context.setdefault(context, []).append(ref)

    # Build groups
    groups = []
    total = 0
    for context, refs in refs_by_context.items():
        groups.append(
            CrossReferenceGroup(
                context=context,
                context_label=CONTEXT_LABELS.get(context, context),
                references=refs,
            )
        )
        total += len(refs)

    return CrossReferencesResponse(
        target_iri=target_iri,
        total=total,
        groups=groups,
    )
