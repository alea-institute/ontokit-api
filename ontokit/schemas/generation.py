"""Pydantic schemas for the suggestion generation and validation API contract.

These types define the request/response shape for Phase 13:
  - Suggestion generation (generate endpoint)
  - Pre-submit validation (validate endpoint)

Design decisions (per 13-CONTEXT.md):
- SuggestionType: 5 discriminated types covering the full suggestion space
- Provenance: tracks how a suggestion originated through the pipeline (D-07)
- ValidationError: structured errors with field + code + message (VALID-05)
- GeneratedSuggestion: core proposal type with auto-validation status embedded (D-09)
- CONTROLLED_RELATIONSHIP_TYPES: 14 controlled types from generative-folio (GEN-05)
- batch_size: configurable 1-10 per request (D-05)
- confidence: normalized 0-1 float or None (D-06)
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Type literals
# ---------------------------------------------------------------------------

# Five suggestion types covering the full ontology editing space (D-07, GEN-09):
#   children    — new child classes under the current class
#   siblings    — new sibling classes at the same level
#   annotations — new annotation property values (rdfs:label, skos:definition, etc.)
#   parents     — new/alternative parent classes
#   edges       — new typed relationships to other ontology entities (GEN-05)
SuggestionType = Literal["children", "siblings", "annotations", "parents", "edges"]

# Provenance tracks how a suggestion moved through the pipeline (D-07):
#   llm-proposed         — fresh LLM output, not yet touched by user
#   user-written         — manually authored by the user (no LLM)
#   user-edited-from-llm — user accepted an LLM suggestion and edited it
Provenance = Literal["llm-proposed", "user-written", "user-edited-from-llm"]

# ---------------------------------------------------------------------------
# Controlled relationship type vocabulary (GEN-05)
# Ported from generative-folio RelationshipType literal
# ---------------------------------------------------------------------------

# All 14 controlled relationship types from alea-institute/generative-folio
# (generative_folio/models/concept.py).  Organized in 4 tiers:
#   Tier 1 — Cross-reference:  seeAlso, contrast
#   Tier 2 — Governance:       isGovernedBy, supersedes, implements
#   Tier 3 — Structural:       locatedIn, appealsTo, isMemberOf, appendedTo
#   Tier 4 — Semantic:         enables, requires, restricts, exemplifies, hasSource
CONTROLLED_RELATIONSHIP_TYPES: list[str] = [
    # Tier 1: Cross-reference (existing)
    "seeAlso",
    "contrast",
    # Tier 2: Governance and authority
    "isGovernedBy",
    "supersedes",
    "implements",
    # Tier 3: Structural / organizational
    "locatedIn",
    "appealsTo",
    "isMemberOf",
    "appendedTo",
    # Tier 4: Semantic / functional
    "enables",
    "requires",
    "restricts",
    "exemplifies",
    "hasSource",
]


# ---------------------------------------------------------------------------
# Core validation type
# ---------------------------------------------------------------------------


class ValidationError(BaseModel):
    """A structured validation failure returned by the server-side gate (VALID-05).

    All validation errors include:
    - field:   which field caused the failure (e.g. "parent_iris", "labels", "iri")
    - code:    machine-readable rule code (e.g. "VALID-01")
    - message: human-readable inline error text for the UI
    """

    field: str
    code: str
    message: str


# ---------------------------------------------------------------------------
# Generated suggestion types
# ---------------------------------------------------------------------------


class GeneratedSuggestion(BaseModel):
    """A single LLM-generated ontology entity suggestion.

    Core proposal type returned by the generate endpoint (D-04).
    Includes embedded validation status so the frontend can render per-entity
    errors without a separate validate call (D-09).
    """

    iri: str
    suggestion_type: SuggestionType
    label: str

    # Optional quality metadata
    definition: str | None = None
    confidence: float | None = None  # Normalized 0-1 score (D-06); None if LLM doesn't provide

    # Provenance (D-07)
    provenance: Provenance = "llm-proposed"

    # Embedded validation state (D-09) — populated by the generation pipeline
    validation_errors: list[ValidationError] = Field(default_factory=list)

    # Duplicate detection results embedded (D-09)
    duplicate_verdict: str = "pass"  # "pass" | "warn" | "block"
    duplicate_candidates: list[dict] = Field(default_factory=list)


class EdgeSuggestion(GeneratedSuggestion):
    """An edge / relationship suggestion between ontology entities (GEN-05).

    Extends GeneratedSuggestion with target entity and controlled relationship type.
    """

    target_iri: str
    relationship_type: str  # Should be one of CONTROLLED_RELATIONSHIP_TYPES


class AnnotationSuggestion(GeneratedSuggestion):
    """An annotation property value suggestion (GEN-03).

    Extends GeneratedSuggestion with the specific annotation property and value.
    """

    property_iri: str  # e.g. "http://www.w3.org/2000/01/rdf-schema#comment"
    value: str
    lang: str | None = None  # BCP-47 language tag; None = language-untagged


# ---------------------------------------------------------------------------
# Generation request / response
# ---------------------------------------------------------------------------


class GenerateSuggestionsRequest(BaseModel):
    """Request body for POST /projects/{id}/generate.

    batch_size is configurable per D-05: 1-10, default 5 (or 3-5 by type).
    """

    class_iri: str
    branch: str = "main"
    suggestion_type: SuggestionType
    batch_size: int = Field(default=5, ge=1, le=10)


class GenerateSuggestionsResponse(BaseModel):
    """Response from the generate endpoint.

    Includes token usage for cost tracking (audit log requires this — Phase 11).
    context_tokens_estimate is the approximate prompt context size; None if not
    computed (some providers don't expose this before the call).
    """

    suggestions: list[GeneratedSuggestion]
    input_tokens: int
    output_tokens: int
    context_tokens_estimate: int | None = None


# ---------------------------------------------------------------------------
# Validation request / response
# ---------------------------------------------------------------------------


class ValidateEntityRequest(BaseModel):
    """Request body for POST /projects/{id}/validate-entity.

    Used by the frontend to validate a manually-authored entity before it
    enters the draft/session flow (D-08). Generated suggestions are
    auto-validated inside the pipeline (D-09) so this endpoint is
    primarily for user-written suggestions.
    """

    entity_iri: str | None = None  # Optional — if not set, IRI will be minted
    label: str
    parent_iris: list[str]
    labels: list[dict]  # Each entry: {"lang": "en", "value": "..."}
    namespace: str | None = None  # Override project namespace for IRI minting


class ValidateEntityResponse(BaseModel):
    """Response from the validate-entity endpoint (VALID-05).

    valid=True only when errors is empty.
    Each error in errors has field, code, and human-readable message.
    """

    valid: bool
    errors: list[ValidationError]
