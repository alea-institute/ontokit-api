"""Suggestion session schemas for request/response validation."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from ontokit.schemas.trust import TrustTier


class SuggestionSessionResponse(BaseModel):
    """Response when creating a suggestion session."""

    session_id: str
    branch: str
    created_at: datetime
    beacon_token: str

    model_config = ConfigDict(from_attributes=True)


class SuggestionSaveRequest(BaseModel):
    """Request body for saving content to a suggestion session."""

    content: str = Field(..., description="Full Turtle source content")
    entity_iri: str = Field(..., description="IRI of the entity being modified")
    entity_label: str = Field(..., description="Human-readable label of the entity")
    mints_entity: bool = Field(
        default=False,
        description=(
            "True when this save introduces a NEW class or property. Minting requires "
            "trusted status (R8); editing existing entities does not."
        ),
    )


class SuggestionSaveResponse(BaseModel):
    """Response after saving to a suggestion session."""

    commit_hash: str
    branch: str
    changes_count: int


class SuggestionSubmitRequest(BaseModel):
    """Request body for submitting a suggestion session as a PR."""

    summary: str | None = Field(default=None, description="Optional summary describing the changes")


class SuggestionSubmitResponse(BaseModel):
    """Response after submitting a suggestion session."""

    pr_number: int
    pr_url: str | None = None
    status: str


class SuggestionUser(BaseModel):
    """User info embedded in session summaries."""

    id: str
    name: str | None = None
    email: str | None = None


class SuggestionSessionSummary(BaseModel):
    """Summary of a suggestion session for list endpoint."""

    session_id: str
    branch: str
    changes_count: int
    last_activity: datetime
    entities_modified: list[str]
    status: str
    pr_number: int | None = None
    pr_url: str | None = None
    github_pr_url: str | None = None
    submitter: SuggestionUser | None = None
    reviewer: SuggestionUser | None = None
    reviewer_feedback: str | None = None
    reviewed_at: datetime | None = None
    revision: int | None = None
    summary: str | None = None
    is_anonymous: bool = False
    # Trust ladder (R9): provenance is self-evident on every review row.
    submitter_tier: TrustTier | None = None
    is_llm_generated: bool = False
    auto_accept_after: datetime | None = None
    auto_accept_halted_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class SuggestionSessionListResponse(BaseModel):
    """Response for listing suggestion sessions."""

    items: list[SuggestionSessionSummary]


class SuggestionBeaconRequest(BaseModel):
    """Request body for beacon save (sendBeacon flush)."""

    session_id: str
    content: str


# --- Review request schemas ---


class SuggestionRejectRequest(BaseModel):
    """Request body for rejecting a suggestion session."""

    reason: str = Field(..., min_length=1, description="Reason for rejection")


class SuggestionRequestChangesRequest(BaseModel):
    """Request body for requesting changes on a suggestion session."""

    feedback: str = Field(..., min_length=1, description="Feedback for the suggester")


class SuggestionResubmitRequest(BaseModel):
    """Request body for resubmitting a suggestion session."""

    summary: str | None = Field(default=None, description="Updated summary")


# --- Triage queue schemas (R9) ---


class SuggestionQueue(StrEnum):
    """Which review queue a listing targets."""

    TRIAGE = "triage"
    REVIEW = "review"


class BulkReviewAction(StrEnum):
    """What a bulk review pass does to each selected session."""

    ACCEPT = "accept"
    DISMISS = "dismiss"


class BulkReviewRequest(BaseModel):
    """Bulk accept or dismiss for the triage queue."""

    session_ids: list[str] = Field(..., min_length=1, max_length=100)
    action: BulkReviewAction
    note: str | None = Field(default=None, max_length=1000)


class BulkReviewFailure(BaseModel):
    """One session that could not be processed, and why."""

    session_id: str
    reason: str


class BulkReviewResponse(BaseModel):
    """Partial-success result: one stale session must not abort the batch."""

    action: BulkReviewAction
    succeeded: list[str]
    failed: list[BulkReviewFailure]


class SuggestionCapabilitiesResponse(BaseModel):
    """What the caller may do on this project, and how trust is earned."""

    tier: TrustTier
    can_suggest: bool
    can_mint_entities: bool
    promotion_threshold: int
    accepted_count: int
    auto_accept_enabled: bool
    auto_accept_quiet_days: int
    verification_required: bool = False
