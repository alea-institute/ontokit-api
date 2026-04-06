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

from typing import Literal

from pydantic import BaseModel

# Verdict thresholds (from PROJECT.md + D-13):
#   block  > 0.95 — hard block, entity must not be created
#   warn   > 0.80 — soft warning, user can override with acknowledgement
#   pass   <= 0.80 — no issue detected
DuplicateVerdict = Literal["block", "warn", "pass"]

# Source indicates where the candidate was found during duplicate search:
#   main     — already committed to the project branch
#   pending  — in an active suggestion session (not yet merged)
#   rejected — previously rejected as a non-duplicate (stored in duplicate_rejections)
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
    score: float
    source: CandidateSource
    branch: str | None = None
    rejection_reason: str | None = None
    canonical_iri: str | None = None


class DuplicateCheckRequest(BaseModel):
    """Request body for POST /projects/{id}/duplicates/check."""

    label: str
    entity_type: str = "class"
    parent_iri: str | None = None
    branch: str | None = None


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
