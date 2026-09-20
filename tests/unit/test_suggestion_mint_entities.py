"""Mint authorization recognizes named individuals independently of submission policy."""

import pytest
from fastapi import HTTPException

from ontokit.services.suggestion_service import SuggestionService

PREFIXES = """
@prefix ex: <http://example.org/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
"""


@pytest.mark.parametrize(
    "assertion",
    [
        "ex:alice a owl:NamedIndividual .",
        "ex:alice a ex:Person .",
        "ex:alice a <https://external.example/Person> .",
        "ex:alice a owl:NamedIndividual, ex:Person, ex:Employee .",
        "ex:alice a owl:Thing .",
        "ex:alice a owl:Nothing .",
        "ex:alice a [ a owl:Restriction; owl:onProperty ex:knows; owl:someValuesFrom ex:Person ] .",
        "ex:alice a [ a owl:Class; owl:unionOf (ex:Person ex:Employee) ] .",
        "ex:alice a owl:Ontology, ex:Person .",
    ],
)
def test_named_individual_identity(assertion: str) -> None:
    assert SuggestionService._declared_entity_iris(PREFIXES + assertion) == {
        "http://example.org/alice"
    }


@pytest.mark.parametrize(
    "structural_type",
    [
        "owl:Ontology",
        "owl:Restriction",
        "owl:Axiom",
        "owl:Annotation",
        "owl:AllDifferent",
        "owl:AllDisjointClasses",
        "owl:AllDisjointProperties",
        "owl:NegativePropertyAssertion",
        "owl:DataRange",
        "owl:DeprecatedClass",
        "owl:DeprecatedProperty",
        "rdfs:Datatype",
        "rdfs:Container",
        "rdfs:ContainerMembershipProperty",
        "rdf:Statement",
        "rdf:List",
        "rdf:Bag",
        "rdf:Seq",
        "rdf:Alt",
    ],
)
def test_structural_typing_alone_is_not_an_individual(structural_type: str) -> None:
    content = PREFIXES + f"ex:structure a {structural_type} ."
    assert SuggestionService._declared_entity_iris(content) == set()
    assert SuggestionService._declared_entity_iris(content + "ex:structure a ex:Person .") == {
        "http://example.org/structure"
    }


@pytest.mark.parametrize(
    "declaration",
    [
        "owl:Class",
        "rdfs:Class",
        "rdf:Property",
        "owl:ObjectProperty",
        "owl:DatatypeProperty",
        "owl:AnnotationProperty",
        "owl:FunctionalProperty",
        "owl:InverseFunctionalProperty",
        "owl:TransitiveProperty",
        "owl:SymmetricProperty",
        "owl:AsymmetricProperty",
        "owl:ReflexiveProperty",
        "owl:IrreflexiveProperty",
        "owl:NamedIndividual",
        "ex:Person",
    ],
)
def test_existing_identity_survives_edits_and_punning(declaration: str) -> None:
    baseline = PREFIXES + f"ex:existing a {declaration} ."
    proposed = (
        baseline
        + 'ex:existing a owl:NamedIndividual, ex:Other; rdfs:label "Edited"; ex:knows ex:other .'
    )
    assert (
        SuggestionService._declared_entity_iris(proposed)
        == (SuggestionService._declared_entity_iris(baseline))
        == {"http://example.org/existing"}
    )


@pytest.mark.parametrize(
    "content",
    [
        "",
        "ex:alice ex:knows ex:bob .",
        'ex:alice rdfs:label "Untyped" .',
        "[] a owl:NamedIndividual, ex:Person .",
        "[] a owl:Restriction .",
        "ex:subject ex:members (ex:alice ex:bob) .",
        'ex:alice a "not a class" .',
    ],
)
def test_untyped_references_and_blank_nodes_do_not_mint(content: str) -> None:
    assert SuggestionService._declared_entity_iris(PREFIXES + content) == set()


def test_first_type_added_to_label_only_subject_is_new_identity() -> None:
    baseline = PREFIXES + 'ex:alice rdfs:label "Alice" .'
    proposed = baseline + "ex:alice a ex:Person ."
    assert SuggestionService._declared_entity_iris(proposed) - (
        SuggestionService._declared_entity_iris(baseline)
    ) == {"http://example.org/alice"}


def test_prefix_aliases_order_and_bytes_preserve_identity() -> None:
    baseline = PREFIXES + "ex:alice a ex:Person, owl:NamedIndividual . ex:Person a owl:Class ."
    proposed = (
        PREFIXES
        + """
        @prefix other: <http://example.org/> .
        other:Person a owl:Class .
        other:alice a owl:NamedIndividual . other:alice a other:Person .
    """
    )
    assert (
        SuggestionService._declared_entity_iris(proposed.encode())
        == (SuggestionService._declared_entity_iris(baseline))
        == {"http://example.org/alice", "http://example.org/Person"}
    )


def test_malformed_turtle_retains_parse_error() -> None:
    with pytest.raises(HTTPException) as exc:
        SuggestionService._declared_entity_iris(PREFIXES + "ex:alice a [")
    assert exc.value.status_code == 422
    assert exc.value.detail == "Suggestion content is not valid Turtle"
