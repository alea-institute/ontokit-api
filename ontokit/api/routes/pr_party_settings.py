"""PR Party reviewer settings: capability, credential, and notification topic.

Mounted at the API root (``/pr-party/...``) rather than under ``/projects``:
PR Party is org-scoped, not project-scoped — it reviews pull requests across
every CatholicOS repository, and no project owns it.

The authorization shape is deliberately asymmetric:

- ``GET /pr-party/me`` answers "may I use PR Party?" and therefore must answer
  for *non*-reviewers too. A non-reviewer gets ``200 {"is_reviewer": false}``.
  403-ing the capability read would leave the web client unable to decide
  whether to render PR Party at all — the question would have no answer.
- Every other route is 403 for a non-reviewer and 401 unauthenticated.

``ntfy_topic`` is a secret (anyone holding it can publish to that reviewer's
phone), so it exists only in the reviewer's own settings read — never in the
capability payload. See ``ontokit/schemas/pr_party.py``.

The write PAT never travels back out: submission is one-way, and the read
surfaces expose only expiry/validation health.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.core.auth import RequiredUser
from ontokit.core.config import settings
from ontokit.core.database import get_db
from ontokit.models.pr_party import PRPartyMergeDefault, PRPartyReviewer
from ontokit.schemas.pr_party import (
    PRPartyCapability,
    PRPartyCredentialHealth,
    PRPartyCredentialRevoked,
    PRPartyCredentialUpdate,
    PRPartyReviewerSettings,
    PRPartyReviewerSettingsUpdate,
)
from ontokit.services.pr_party_credentials import (
    GITHUB_TOKEN_SETTINGS_URL,
    CredentialRejected,
    CredentialValidationUnavailable,
    PRPartyCredentialService,
    credential_health,
    get_generation_token_status,
    is_degraded,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def get_credential_service(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PRPartyCredentialService:
    """Dependency for the reviewer registry / credential service."""
    return PRPartyCredentialService(db)


CredentialService = Annotated[PRPartyCredentialService, Depends(get_credential_service)]


async def _require_reviewer(user: RequiredUser, service: CredentialService) -> PRPartyReviewer:
    """Registered reviewers only (KTD12) — the registry is the whole allowlist."""
    reviewer = await service.get_reviewer(user.id)
    if reviewer is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="PR Party is limited to registered reviewers.",
        )
    return reviewer


def _settings_payload(reviewer: PRPartyReviewer) -> PRPartyReviewerSettings:
    return PRPartyReviewerSettings(
        github_login=reviewer.github_login,
        merge_default=PRPartyMergeDefault(reviewer.merge_default),
        ntfy_topic=reviewer.ntfy_topic,
        ntfy_base_url=settings.pr_party_ntfy_base_url,
    )


@router.get("/me", response_model=PRPartyCapability)
async def get_pr_party_capability(
    user: RequiredUser,
    service: CredentialService,
) -> PRPartyCapability:
    """Whether the caller is a reviewer, and whether PR Party can actuate for them.

    A missing or expired credential is reported as ``degraded`` — not an error.
    The dashboard is still fully readable in that state (R12); only actuation
    downgrades to recorded intent.
    """
    reviewer = await service.get_reviewer(user.id)
    if reviewer is None:
        return PRPartyCapability(is_reviewer=False)

    health = credential_health(await service.get_credential(reviewer.id))

    return PRPartyCapability(
        is_reviewer=True,
        degraded=is_degraded(health),
        github_login=reviewer.github_login,
        credential=health,
        generation_token=await get_generation_token_status(),
    )


@router.put("/credential", response_model=PRPartyCredentialHealth)
async def save_credential(
    body: PRPartyCredentialUpdate,
    user: RequiredUser,
    service: CredentialService,
) -> PRPartyCredentialHealth:
    """Submit or rotate this reviewer's GitHub write PAT (KTD13).

    The token is proven to authenticate as the registered login and to perform a
    real read before anything is stored, so a bad rotation leaves the working
    credential untouched. A mismatch is a 400 with nothing written.

    TODO(U6/M2): per-user rate limiting for this route belongs on the shared PR
    Party limiter U6 introduces. A fail-closed limiter of its own (the
    ``trust_rate_limiter`` pattern) would be disproportionate here: the caller
    is one of a handful of config-provisioned reviewers, the global 100/min per
    IP limit already applies, and failing closed on a Redis blip would lock a
    reviewer out of connecting the very credential that un-degrades them.
    """
    reviewer = await _require_reviewer(user, service)

    try:
        credential = await service.save_credential(reviewer, body.token)
    except CredentialRejected as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except CredentialValidationUnavailable as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e)) from e

    health = credential_health(credential)
    if health is None:  # pragma: no cover — save_credential always returns a row
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Credential was saved but could not be read back.",
        )
    logger.info("PR Party credential stored for reviewer %s", reviewer.zitadel_user_id)
    return health


@router.delete("/credential", response_model=PRPartyCredentialRevoked)
async def delete_credential(
    user: RequiredUser,
    service: CredentialService,
) -> PRPartyCredentialRevoked:
    """Forget our copy of the PAT.

    This does NOT revoke the token on GitHub — the app holds no authority to do
    that on the reviewer's behalf (KTD13) — so the response says so and points
    at the page where they can finish the job.
    """
    reviewer = await _require_reviewer(user, service)
    existed = await service.delete_credential(reviewer)
    return PRPartyCredentialRevoked(revoked_locally=existed, revoke_url=GITHUB_TOKEN_SETTINGS_URL)


@router.get("/settings", response_model=PRPartyReviewerSettings)
async def get_reviewer_settings(
    user: RequiredUser,
    service: CredentialService,
) -> PRPartyReviewerSettings:
    """The caller's own PR Party settings, including their ntfy topic."""
    reviewer = await _require_reviewer(user, service)
    return _settings_payload(reviewer)


@router.put("/settings", response_model=PRPartyReviewerSettings)
async def update_reviewer_settings(
    body: PRPartyReviewerSettingsUpdate,
    user: RequiredUser,
    service: CredentialService,
) -> PRPartyReviewerSettings:
    """Update the caller's OWN settings (R23).

    The body carries no reviewer identifier: the target is always the
    authenticated caller, so there is no field an attacker could set to
    redirect the write. Unset fields are left alone; an empty ``ntfy_topic``
    clears it.
    """
    reviewer = await _require_reviewer(user, service)
    updates = body.model_dump(exclude_unset=True)
    reviewer = await service.update_settings(reviewer, updates)
    return _settings_payload(reviewer)
