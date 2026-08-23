"""Pydantic schemas for the duplicate detection API contract.

These types define the request/response shape for the whole-ontology duplicate check
endpoint (Plan 12-02) and are consumed by Phase 13-14 frontends.

Design decisions (per D-13):
- verdict: "block" (>0.95 composite) | "warn" (>0.80) | "pass" (below 0.80)
- composite_score: weighted blend of exact + semantic + structural scores
- score_breakdown: raw component scores for transparency and debugging
- candidates: top-k similar entities with provenance (main branch, pending session, rejected)
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Verdict thresholds (from PROJECT.md + D-13):
#   block  > 0.95 — hard block, entity must not be created
#   warn   > 0.80 — soft warning, user can override with acknowledgement
#   pass   <= 0.80 — no issue detected
DuplicateVerdict = Literal["block", "warn", "pass"]

# Source indicates where the candidate was found during duplicate search:
#   main     — already committed to the project branch
#   pending  — in an active suggestion session (not yet merged)
#   rejected — candidate came from a rejected suggestion-session branch
CandidateSource = Literal["main", "pending", "rejected"]


class ScoreBreakdown(BaseModel):
    """Raw component scores that make up the composite duplicate score.

    All values are in [0.0, 1.0]:
    - exact: character-level match (Levenshtein / exact string equality)
    - semantic: cosine similarity between embedding vectors (HNSW ANN)
    - structural: similarity of structural position (parent, siblings, properties)
    """

    exact: float
    semantic: float
    structural: float


class DuplicateCandidate(BaseModel):
    """A single candidate that may be a duplicate of the entity being checked."""

    iri: str
    label: str
    entity_type: str = "class"
    score: float
    source: CandidateSource
    branch: str | None = None
    rejection_reason: str | None = None
    canonical_iri: str | None = None


class DuplicateCheckRequest(BaseModel):
    """Request body for POST /projects/{id}/duplicates/check.

    Note: there is deliberately no ``branch`` field. Duplicate detection always
    searches across ALL branches (DEDUP-08) — a duplicate on any branch matters —
    so a per-request branch scope would be silently ignored. The branch a
    candidate was found on is reported back on :class:`DuplicateCandidate.branch`.
    """

    label: str
    entity_type: str = "class"
    parent_iri: str | None = None
    proposed_iri: str | None = None
    suggestion_session_id: UUID | None = None


class DistinctDecisionMarkRequest(BaseModel):
    """Create or idempotently reuse a human decision that two IRIs are distinct."""

    proposed_iri: str = Field(min_length=1, max_length=2000)
    label: str = Field(min_length=1, max_length=2000)
    candidate_iri: str = Field(min_length=1, max_length=2000)
    entity_type: str = Field(default="class", min_length=1, max_length=100)
    parent_iri: str | None = Field(default=None, max_length=2000)
    suggestion_session_id: UUID | None = None
    reason: str = Field(min_length=1, max_length=4000)

    @field_validator(
        "proposed_iri", "label", "candidate_iri", "entity_type", "reason", mode="before"
    )
    @classmethod
    def reject_blank_text(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            if not value:
                raise ValueError("must not be blank")
        return value

    @model_validator(mode="after")
    def reject_self_pair(self) -> DistinctDecisionMarkRequest:
        if self.proposed_iri == self.candidate_iri:
            raise ValueError("an entity cannot be marked distinct from itself")
        return self


class DistinctDecisionResponse(BaseModel):
    """Auditable, fingerprint-bound decision that an IRI pair is distinct."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    iri_a: str
    iri_b: str
    fingerprint_a: str
    fingerprint_b: str
    reason: str
    marked_by: str
    marked_at: datetime
    suggestion_session_id: UUID | None
    revoked_at: datetime | None
    revoked_by: str | None
    superseded_by_id: UUID | None

    @property
    def active(self) -> bool:
        return self.revoked_at is None


class DuplicateCheckResponse(BaseModel):
    """Response for the duplicate check endpoint.

    verdict drives the UI gate:
    - "block"  → hard block in editor/suggestion form (cannot proceed)
    - "warn"   → amber warning with acknowledge option
    - "pass"   → green light, proceed normally
    """

    verdict: DuplicateVerdict
    composite_score: float
    score_breakdown: ScoreBreakdown
    candidates: list[DuplicateCandidate]
    suppressed_decisions: list[DistinctDecisionResponse] = Field(default_factory=list)
