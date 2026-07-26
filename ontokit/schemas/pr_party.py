"""Pydantic schemas for PR Party.

U2 owns the *settings and capability* models below. The queue, card, brief, and
verdict payloads land here later (U15) — this module is deliberately additive,
so extending it never has to touch what the settings surface already promises.

One rule governs the shapes here: **the capability payload carries no secrets.**
``GET /pr-party/me`` is the widest-read PR Party response (the web client hits
it on every page load to gate its nav), so ``ntfy_topic`` — a reviewer's private
notification channel — is structurally absent from :class:`PRPartyCapability`
and lives only in :class:`PRPartyReviewerSettings`, which is read by its owner.
"""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from ontokit.models.pr_party import PRPartyMergeDefault

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
