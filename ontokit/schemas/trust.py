"""Schemas for the contribution trust ladder (R4-R11)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from ontokit.models.suggestion_outcome import SuggestionOutcomeType


class TrustTier(StrEnum):
    """The rungs of the contribution ladder.

    Ordered from least to most privileged. ``reviewer`` sits above ``trusted``
    and covers the existing owner/admin/editor roles and superadmins (KTD4) —
    the ladder governs suggester/no-role members and never demotes staff.
    """

    ANONYMOUS = "anonymous"
    UNTRUSTED = "untrusted"
    TRUSTED = "trusted"
    REVIEWER = "reviewer"


class TrustOverride(StrEnum):
    """Admin decision that outranks auto-promotion, and is sticky (KTD1)."""

    NONE = "none"
    GRANTED = "granted"
    REFUSED = "refused"
    REVOKED = "revoked"


class SuggestionCapabilities(BaseModel):
    """What the caller may do on this project, and how trust is earned.

    Single payload for the editor's affordance gating (AE2) and the future
    contributor-standing UI. Read from the same ``resolve_tier`` the server-side
    gate uses, so the affordance and the enforcement can never disagree (KTD3).
    """

    tier: TrustTier
    can_suggest: bool
    can_mint_entities: bool
    promotion_threshold: int
    accepted_count: int
    auto_accept_enabled: bool
    auto_accept_quiet_days: int
    verification_required: bool = Field(
        default=False,
        description="True when the caller's next suggestion needs a human-verification token.",
    )


class MemberTrustResponse(BaseModel):
    """A member's trust state, as returned to project admins."""

    user_id: str
    role: str
    tier: TrustTier
    is_trusted: bool
    trust_override: TrustOverride
    trust_granted_at: datetime | None = None
    trust_granted_by: str | None = None
    accepted_count: int


class MemberTrustUpdate(BaseModel):
    """Admin grant / refuse / revoke / clear."""

    trust_override: TrustOverride


class ProjectTrustSettings(BaseModel):
    """Per-project ladder configuration (R6, R11)."""

    trust_promotion_threshold: int = Field(ge=1, le=1000)
    auto_accept_enabled: bool
    auto_accept_quiet_days: int = Field(ge=1, le=365)


class ProjectTrustSettingsUpdate(BaseModel):
    """Partial update of the per-project ladder configuration."""

    trust_promotion_threshold: int | None = Field(default=None, ge=1, le=1000)
    auto_accept_enabled: bool | None = Field(default=None)
    auto_accept_quiet_days: int | None = Field(default=None, ge=1, le=365)


class SuggestionOutcomeItem(BaseModel):
    """Stored audit facts for one terminal suggestion outcome."""

    user_id: str
    is_anonymous: bool
    submitter_name: str | None
    submitter_email: str | None
    snapshot_tier: TrustTier | None
    snapshot_role: str | None
    snapshot_captured_at: datetime | None
    outcome: SuggestionOutcomeType
    decided_by: str | None
    decided_by_name: str | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SuggestionOutcomeListResponse(BaseModel):
    """Keyset-paginated suggestion outcome audit trail."""

    items: list[SuggestionOutcomeItem]
    total: int = Field(
        description=(
            "Live, unpaginated display count; it can exceed the rows reachable during an "
            "in-flight cursor walk."
        )
    )
    next_cursor: str | None = Field(
        description="A value of None is the sole signal that the end of the list was reached."
    )
