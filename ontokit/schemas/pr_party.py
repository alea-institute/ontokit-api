"""Pydantic schemas for PR Party.

U2 owns the *settings and capability* models; U15 adds the *queue and card*
read models below them. The module is deliberately additive, so extending it
never has to touch what an earlier surface already promises.

Two rules govern the shapes here:

- **The capability payload carries no secrets.** ``GET /pr-party/me`` is the
  widest-read PR Party response (the web client hits it on every page load to
  gate its nav), so ``ntfy_topic`` — a reviewer's private notification channel
  — is structurally absent from :class:`PRPartyCapability` and lives only in
  :class:`PRPartyReviewerSettings`, which is read by its owner.
- **The read models ship conclusions, not ingredients (KTD19).** Readiness,
  staleness, parking, and own-vs-counterpart are all computed server-side and
  serialized as plain data. A client that re-derives any of them from
  ``brief_status`` / ``checks_rollup`` / ``head_sha`` has forked the rule, and
  the fork will drift. Every field below is either a fact from GitHub or a
  decision this API already made.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Final

from pydantic import BaseModel, Field, field_validator

from ontokit.models.pr_party import (
    PRPartyActionKind,
    PRPartyActionStatus,
    PRPartyAuthorKind,
    PRPartyBriefStatus,
    PRPartyMergeDefault,
)

#: ntfy topics become a URL path segment (``{base}/{topic}``). Restricting them
#: to this alphabet is what keeps a saved topic from escaping its segment and
#: pointing the notifier somewhere else.
NTFY_TOPIC_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class PRPartyCredentialHealth(BaseModel):
    """The state of a reviewer's stored write PAT — never the PAT itself.

    ``expires_soon`` is the T-30 warning: a fine-grained PAT expires on a fixed
    date, and a reviewer who finds out at the moment they try to merge has
    already lost the merge.
    """

    expires_at: datetime | None = None
    last_validated_at: datetime | None = None
    last_error: str | None = None
    expired: bool = False
    expires_soon: bool = False


class PRPartyGenerationTokenStatus(BaseModel):
    """Health of the *shared* read-only generation token (KTD13).

    Computed live (with a short in-process cache) rather than stored: there is
    no per-token row for a value that lives in the deployment's environment.
    """

    expires_at: datetime | None = None
    last_error: str | None = None


class PRPartyCapability(BaseModel):
    """Answer to "may I use PR Party, and does it currently work?".

    A non-reviewer gets ``is_reviewer=False`` with everything else empty — a
    200, not a 403, because the web client uses this to decide whether to render
    PR Party at all.

    ``degraded`` is true when the reviewer is registered but cannot actuate:
    no credential, a stored error, or an expired PAT. The dashboard still
    renders in that state (R12) — verdicts record as intent instead of posting.
    """

    is_reviewer: bool
    degraded: bool = False
    github_login: str | None = None
    credential: PRPartyCredentialHealth | None = None
    generation_token: PRPartyGenerationTokenStatus | None = None


class PRPartyReviewerSettings(BaseModel):
    """A reviewer's own settings. Readable only by that reviewer (R23)."""

    github_login: str
    merge_default: PRPartyMergeDefault
    ntfy_topic: str | None = None
    ntfy_base_url: str


class PRPartyReviewerSettingsUpdate(BaseModel):
    """Partial update of the caller's OWN settings row.

    There is deliberately no reviewer identifier in this body (R23): the target
    is always the authenticated caller, so a body field can never redirect the
    write at someone else. Unset fields are left alone; an explicit empty
    ``ntfy_topic`` clears it.
    """

    merge_default: PRPartyMergeDefault | None = None
    ntfy_topic: str | None = None

    @field_validator("ntfy_topic")
    @classmethod
    def _validate_topic(cls, value: str | None) -> str | None:
        if value is None:
            return None
        topic = value.strip()
        if not topic:
            return None
        if not NTFY_TOPIC_PATTERN.match(topic):
            raise ValueError(
                "An ntfy topic may contain only letters, digits, hyphens, and "
                "underscores (max 64 characters)."
            )
        return topic


class PRPartyCredentialUpdate(BaseModel):
    """Submit or rotate the reviewer's GitHub write PAT.

    The token is validated against GitHub *before* anything is stored, so a bad
    rotation leaves the working credential in place.
    """

    token: str = Field(min_length=1, description="A GitHub PAT with write access to the org.")


class PRPartyCredentialRevoked(BaseModel):
    """Result of deleting the stored credential.

    ``revoke_url`` matters: deleting our copy does not revoke the token on
    GitHub, and the app has no way to do that for the reviewer (KTD13). The
    honest response is to say so and point at the page that can.
    """

    revoked_locally: bool
    revoke_url: str


# ---------------------------------------------------------------------------
# Verdict vocabulary (U15 reads it; U6 writes it)
# ---------------------------------------------------------------------------

#: The values ``pr_party_action.verdict`` may hold. U1 typed that column as a
#: bare ``String(30)`` because the verdict path did not exist yet; the read API
#: has to interpret it, so the vocabulary is pinned here rather than left to
#: whatever U6 happens to write.
#:
#: ``discuss_live`` is the one that is *not* a GitHub review event. It records
#: "we will talk about this in person", and the queue turns it into a parked
#: card (see :attr:`PRPartyQueueCard.parked`). U6 records it as an ordinary
#: review-kind action row — ``action_kind='review'``, ``verdict='discuss_live'``,
#: ``status='succeeded'``, at the head SHA the reviewer was looking at — with no
#: GitHub call behind it. Nothing else parks a card: there is no row-level park
#: column, deliberately, because a park is one reviewer's stance on one
#: revision and dies with the next push (R24, C1).
PR_PARTY_VERDICT_APPROVE: Final = "approve"
PR_PARTY_VERDICT_REQUEST_CHANGES: Final = "request_changes"
PR_PARTY_VERDICT_COMMENT: Final = "comment"
PR_PARTY_VERDICT_DISCUSS_LIVE: Final = "discuss_live"

#: Statuses that mean "this reviewer has committed to something, but GitHub has
#: not confirmed it yet" — a pending call in flight, or a verdict recorded as
#: intent because the reviewer's PAT could not deliver it (R12).
PR_PARTY_UNSETTLED_STATUSES: Final[frozenset[PRPartyActionStatus]] = frozenset(
    {PRPartyActionStatus.PENDING, PRPartyActionStatus.DEGRADED_INTENT}
)


# ---------------------------------------------------------------------------
# Queue and card read models (U15)
# ---------------------------------------------------------------------------


class PRPartyReadiness(BaseModel):
    """Whether a card is ready to be *decided on*, and if not, why not (R17).

    Computed server-side and shipped as data. ``reason`` is plain language
    aimed at the reviewer — not an error code, not a field name — because it is
    rendered verbatim.

    "Ready" is about the verdict buttons only. Ask-a-question and discuss-live
    stay available on every card regardless: a reviewer who wants to talk about
    a PR should never have to wait for CI to say so.
    """

    ready: bool
    reason: str | None = None


class PRPartyActionState(BaseModel):
    """The caller's latest actuation of one kind against one PR.

    Bodies are deliberately absent: the queue tells a reviewer *what they did*,
    and the prose they wrote lives on GitHub where the conversation is.
    """

    kind: PRPartyActionKind
    verdict: str | None = None
    status: PRPartyActionStatus
    head_sha: str
    override: bool = False
    created_at: datetime | None = None


class PRPartyOtherReviewerState(BaseModel):
    """What the *other* reviewer has done — two booleans and nothing else.

    R24 makes the two reviewers independent, but not blind: knowing a
    counterpart already approved changes whether you bother. Knowing *what they
    wrote* does not, and shipping it would leak one reviewer's in-flight
    reasoning into the other's dashboard. Both flags are scoped to the current
    head SHA, so a superseded approval reads as absent.
    """

    has_approved: bool = False
    has_pending_intent: bool = False


class PRPartyQueueCard(BaseModel):
    """One PR as the dashboard sees it, projected for the calling reviewer.

    ``author_kind`` is caller-relative: the row stores ``counterpart`` for any
    registry-member author, and ``own`` exists only here, derived by matching
    the stored author identity against the caller (R18/R19).

    ``title`` is currently always ``None``: U1's schema has no column for the
    PR title, and this API refuses to invent one out of brief prose. Clients
    fall back to ``{repo_full_name}#{pr_number}``, which is always present. When
    intake starts persisting the title, this field carries it with no shape
    change.
    """

    card_id: uuid.UUID
    repo_full_name: str
    pr_number: int
    title: str | None = None

    author_kind: PRPartyAuthorKind
    author_github_login: str | None = None
    #: R18: GitHub will not let you approve your own PR, so the card says so
    #: before the reviewer discovers it as a 422.
    read_only: bool = False

    state: str
    head_sha: str
    #: R4: verbatim, including ``"computing"``. ``None`` means unknown — never
    #: render either as "not mergeable".
    mergeable_state: str | None = None
    checks_rollup: str | None = None

    brief_status: PRPartyBriefStatus
    brief_truncated: bool = False
    ready_at: datetime | None = None
    updated_at_github: datetime | None = None

    #: Built from ``(repo_full_name, pr_number)`` — PR facts, never from brief
    #: text. A failed brief still hands the reviewer working links.
    pr_url: str
    diff_url: str

    readiness: PRPartyReadiness
    #: The caller's latest action per kind, newest first. At most one per kind.
    actions: list[PRPartyActionState] = Field(default_factory=list)
    other_reviewer: PRPartyOtherReviewerState = Field(default_factory=PRPartyOtherReviewerState)
    #: C1: the caller's latest live review was cast against an older revision.
    stale: bool = False
    #: The caller's latest live review on *this* revision was ``discuss_live``.
    parked: bool = False


class PRPartyQueueResponse(BaseModel):
    """The caller's whole queue at one instant.

    ``generated_at`` is what lets the client say "as of 14:02" instead of
    implying the view is live; the endpoint is ``no-store``, so this is the only
    freshness signal there is.
    """

    generated_at: datetime
    cards: list[PRPartyQueueCard] = Field(default_factory=list)


class PRPartyCardDetail(PRPartyQueueCard):
    """One card opened: everything the queue carries, plus the brief itself.

    Strictly a superset of :class:`PRPartyQueueCard`, so a client can type one
    model and widen it — opening a card never re-shapes what it already showed.

    R21: ``brief_what`` / ``brief_why`` are ``str`` and never ``None``. Absent
    prose is the empty string, so there is exactly one empty representation and
    exactly one rendering path. They are plain text: no markup, no sanitized
    HTML, nothing a client should ever hand to ``innerHTML``.
    """

    brief_what: str = ""
    brief_why: str = ""
    brief_decisions: list[str] = Field(default_factory=list)
    #: Pre-allowlisted server-side to the PR's own repository — see
    #: :func:`ontokit.services.pr_party_brief.filter_links`.
    brief_links: list[str] = Field(default_factory=list)
    #: Plain-language note when the brief was shortened, written here rather
    #: than left for the client to phrase.
    truncated_note: str | None = None

    brewing_since: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    #: Placeholder for U7's Q&A thread. Present now — and empty — so the UI
    #: contract does not move when U7 lands: U7 narrows ``Any`` to its entry
    #: model without renaming the field or changing its cardinality.
    qa_thread: list[Any] = Field(default_factory=list)
