"""Resolve the credential used to push the GitHub mirror (R3, KD6, KTD15).

One system-owned machine identity pushes every mirror. Per-user Personal Access
Tokens are retired: a lay contributor does not have one and should never need
one, and per-user credentials made the mirror's push history depend on whichever
member happened to connect the integration.

The per-user PAT remains a *fallback* for one release so an in-flight deployment
that has not yet been given a system token keeps syncing — with a deprecation
warning each time, so the gap is visible in the logs rather than silent.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.core.config import settings
from ontokit.core.encryption import decrypt_token
from ontokit.models.pull_request import GitHubIntegration
from ontokit.models.user_github_token import UserGitHubToken
from ontokit.services.demo_target_authorizer import authorize_integration_target

logger = logging.getLogger(__name__)

# Stable, greppable marker for the deprecated per-user fallback.
PAT_FALLBACK_EVENT = "github_mirror_per_user_pat_fallback"


async def resolve_mirror_credential(db: AsyncSession, integration: GitHubIntegration) -> str | None:
    """Return the token to authenticate the mirror push, or None when absent.

    A missing or undecryptable credential returns ``None``. Target-policy
    failures remain typed ``DemoTargetDenied`` exceptions so each caller can
    persist and surface the authorization refusal instead of misreporting it as
    an ordinary missing credential.
    """
    authorization = await authorize_integration_target(
        db, integration, operation="mirror credential resolution"
    )

    if authorization.token:
        return authorization.token
    if settings.github_mirror_token:
        return settings.github_mirror_token

    user_id = integration.connected_by_user_id
    if not user_id:
        logger.warning(
            "GitHub mirror identity unavailable for project %s: no system identity or "
            "connecting user",
            integration.project_id,
        )
        return None

    result = await db.execute(select(UserGitHubToken).where(UserGitHubToken.user_id == user_id))
    token_row = result.scalar_one_or_none()
    if token_row is None:
        logger.warning(
            "GitHub mirror identity unavailable for project %s: no usable stored identity",
            integration.project_id,
        )
        return None

    try:
        token = decrypt_token(token_row.encrypted_token)
    except Exception:
        logger.warning(
            "Stored GitHub mirror identity could not be read for project %s",
            integration.project_id,
        )
        return None

    logger.warning(
        "DEPRECATED %s: project %s is syncing with a per-user PAT because "
        "GITHUB_MIRROR_TOKEN is unset. Configure the system mirror identity — per-user "
        "tokens are being retired.",
        PAT_FALLBACK_EVENT,
        integration.project_id,
        extra={"event": PAT_FALLBACK_EVENT, "project_id": str(integration.project_id)},
    )
    return token
