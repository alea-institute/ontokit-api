"""Tests for generation.py Pydantic schemas — Plan 13-01 (Task 1 RED phase).

These tests verify the schema contract for Phase 13 suggestion generation
and validation guardrails (VALID-01..06, GEN-05..09).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from ontokit.schemas.generation import (
    CONTROLLED_RELATIONSHIP_TYPES,
    AnnotationSuggestion,
    EdgeSuggestion,
    GeneratedSuggestion,
    GenerateSuggestionsRequest,
    GenerateSuggestionsResponse,
    Provenance,
    SuggestionType,
    ValidateEntityRequest,
    ValidateEntityResponse,
    ValidationError,
)


# ---------------------------------------------------------------------------
# Literal type tests
# ---------------------------------------------------------------------------


def test_suggestion_type_literal():
    """SuggestionType contains all 5 required values."""
    # Pydantic Literal aliases can be tested by attempting assignment
    valid_types = ["children", "siblings", "annotations", "parents", "edges"]
    for t in valid_types:
        req = GenerateSuggestionsRequest(
            class_iri="http://example.org#Foo",
            suggestion_type=t,
        )
        assert req.suggestion_type == t


def test_provenance_literal():
    """Provenance literal contains all 3 required values."""
    valid_provenances = ["llm-proposed", "user-written", "user-edited-from-llm"]
    for p in valid_provenances:
        sug = GeneratedSuggestion(
            iri="http://example.org#A",
            suggestion_type="children",
            label="Test",
            provenance=p,
        )
        assert sug.provenance == p


# ---------------------------------------------------------------------------
# ValidationError schema
# ---------------------------------------------------------------------------


def test_validation_error_has_required_fields():
    """ValidationError schema has field, code, message."""
    ve = ValidationError(field="parent_iris", code="VALID-01", message="Required.")
    assert ve.field == "parent_iris"
    assert ve.code == "VALID-01"
    assert ve.message == "Required."


# ---------------------------------------------------------------------------
# GeneratedSuggestion
# ---------------------------------------------------------------------------


def test_generated_suggestion_defaults():
    """GeneratedSuggestion has sane defaults for optional fields."""
    sug = GeneratedSuggestion(
        iri="http://example.org#NewClass",
        suggestion_type="children",
        label="New Child",
    )
    assert sug.provenance == "llm-proposed"
    assert sug.validation_errors == []
    assert sug.duplicate_verdict == "pass"
    assert sug.duplicate_candidates == []
    assert sug.definition is None
    assert sug.confidence is None


def test_generated_suggestion_accepts_confidence():
    """GeneratedSuggestion accepts float confidence in [0, 1]."""
    sug = GeneratedSuggestion(
        iri="http://example.org#NewClass",
        suggestion_type="annotations",
        label="A",
        confidence=0.87,
    )
    assert sug.confidence == pytest.approx(0.87)


# ---------------------------------------------------------------------------
# EdgeSuggestion / AnnotationSuggestion subtypes
# ---------------------------------------------------------------------------


def test_edge_suggestion_has_target_and_type():
    """EdgeSuggestion extends GeneratedSuggestion with target_iri and relationship_type."""
    edge = EdgeSuggestion(
        iri="http://example.org#Foo",
        suggestion_type="edges",
        label="Foo",
        target_iri="http://example.org#Bar",
        relationship_type="seeAlso",
    )
    assert edge.target_iri == "http://example.org#Bar"
    assert edge.relationship_type == "seeAlso"


def test_annotation_suggestion_has_property_and_value():
    """AnnotationSuggestion extends GeneratedSuggestion with property_iri, value, lang."""
    ann = AnnotationSuggestion(
        iri="http://example.org#Foo",
        suggestion_type="annotations",
        label="Foo",
        property_iri="http://www.w3.org/2000/01/rdf-schema#comment",
        value="A legal concept.",
        lang="en",
    )
    assert ann.property_iri == "http://www.w3.org/2000/01/rdf-schema#comment"
    assert ann.value == "A legal concept."
    assert ann.lang == "en"


def test_annotation_suggestion_lang_optional():
    """AnnotationSuggestion lang defaults to None."""
    ann = AnnotationSuggestion(
        iri="http://example.org#Foo",
        suggestion_type="annotations",
        label="Foo",
        property_iri="http://www.w3.org/2004/02/skos/core#definition",
        value="Definition text.",
    )
    assert ann.lang is None


# ---------------------------------------------------------------------------
# GenerateSuggestionsRequest
# ---------------------------------------------------------------------------


def test_generate_request_defaults():
    """GenerateSuggestionsRequest branch defaults to 'main', batch_size to 5."""
    req = GenerateSuggestionsRequest(
        class_iri="http://example.org#LegalConcept",
        suggestion_type="siblings",
    )
    assert req.branch == "main"
    assert req.batch_size == 5


def test_generate_request_batch_size_bounds():
    """batch_size must be between 1 and 10 (ge=1, le=10)."""
    # Valid boundaries
    req_min = GenerateSuggestionsRequest(
        class_iri="http://example.org#X",
        suggestion_type="children",
        batch_size=1,
    )
    req_max = GenerateSuggestionsRequest(
        class_iri="http://example.org#X",
        suggestion_type="children",
        batch_size=10,
    )
    assert req_min.batch_size == 1
    assert req_max.batch_size == 10

    # Invalid: too small
    with pytest.raises(PydanticValidationError):
        GenerateSuggestionsRequest(
            class_iri="http://example.org#X",
            suggestion_type="children",
            batch_size=0,
        )

    # Invalid: too large
    with pytest.raises(PydanticValidationError):
        GenerateSuggestionsRequest(
            class_iri="http://example.org#X",
            suggestion_type="children",
            batch_size=11,
        )


# ---------------------------------------------------------------------------
# GenerateSuggestionsResponse
# ---------------------------------------------------------------------------


def test_generate_response_structure():
    """GenerateSuggestionsResponse has suggestions list and token counts."""
    resp = GenerateSuggestionsResponse(
        suggestions=[],
        input_tokens=150,
        output_tokens=300,
        context_tokens_estimate=2500,
    )
    assert resp.suggestions == []
    assert resp.input_tokens == 150
    assert resp.output_tokens == 300
    assert resp.context_tokens_estimate == 2500


def test_generate_response_context_tokens_optional():
    """GenerateSuggestionsResponse context_tokens_estimate is optional (None)."""
    resp = GenerateSuggestionsResponse(
        suggestions=[],
        input_tokens=100,
        output_tokens=200,
    )
    assert resp.context_tokens_estimate is None


# ---------------------------------------------------------------------------
# ValidateEntityRequest / Response
# ---------------------------------------------------------------------------


def test_validate_entity_request_structure():
    """ValidateEntityRequest captures all required fields."""
    req = ValidateEntityRequest(
        label="New Concept",
        parent_iris=["http://example.org#ParentClass"],
        labels=[{"lang": "en", "value": "New Concept"}],
    )
    assert req.label == "New Concept"
    assert req.entity_iri is None
    assert req.namespace is None


def test_validate_entity_response_structure():
    """ValidateEntityResponse has valid bool and error list."""
    resp = ValidateEntityResponse(valid=True, errors=[])
    assert resp.valid is True
    assert resp.errors == []

    resp_invalid = ValidateEntityResponse(
        valid=False,
        errors=[ValidationError(field="parent_iris", code="VALID-01", message="Required.")],
    )
    assert resp_invalid.valid is False
    assert len(resp_invalid.errors) == 1
    assert resp_invalid.errors[0].code == "VALID-01"


# ---------------------------------------------------------------------------
# CONTROLLED_RELATIONSHIP_TYPES
# ---------------------------------------------------------------------------


def test_controlled_relationship_types_count():
    """CONTROLLED_RELATIONSHIP_TYPES has exactly 14 entries."""
    assert len(CONTROLLED_RELATIONSHIP_TYPES) == 14


def test_controlled_relationship_types_contains_see_also():
    """CONTROLLED_RELATIONSHIP_TYPES contains 'seeAlso' (from generative-folio)."""
    assert "seeAlso" in CONTROLLED_RELATIONSHIP_TYPES


def test_controlled_relationship_types_contains_all_tiers():
    """All 4 tiers of relationship types are present."""
    # Tier 1
    assert "seeAlso" in CONTROLLED_RELATIONSHIP_TYPES
    assert "contrast" in CONTROLLED_RELATIONSHIP_TYPES
    # Tier 2
    assert "isGovernedBy" in CONTROLLED_RELATIONSHIP_TYPES
    assert "supersedes" in CONTROLLED_RELATIONSHIP_TYPES
    assert "implements" in CONTROLLED_RELATIONSHIP_TYPES
    # Tier 3
    assert "locatedIn" in CONTROLLED_RELATIONSHIP_TYPES
    assert "isMemberOf" in CONTROLLED_RELATIONSHIP_TYPES
    # Tier 4
    assert "enables" in CONTROLLED_RELATIONSHIP_TYPES
    assert "requires" in CONTROLLED_RELATIONSHIP_TYPES
    assert "restricts" in CONTROLLED_RELATIONSHIP_TYPES
    assert "exemplifies" in CONTROLLED_RELATIONSHIP_TYPES
    assert "hasSource" in CONTROLLED_RELATIONSHIP_TYPES
