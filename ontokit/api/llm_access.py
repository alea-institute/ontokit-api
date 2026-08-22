"""Shared authorization and rate-limit boundary for LLM-backed API calls."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status

from ontokit.core.auth import CurrentUser, require_authenticated_identity
from ontokit.services.llm import check_llm_access
from ontokit.services.llm.rate_limiter import FAIL_OPEN_EVENT, check_rate_limit

logger = logging.getLogger(__name__)


def get_rate_limit_redis() -> Any:
    """Return the application's shared Redis pool, if startup created one."""
    try:
        from ontokit.main import redis_pool

        return redis_pool
    except (ImportError, AttributeError) as exc:
        logger.warning(
            "Rate-limit Redis pool unavailable (%s) — rate limiting will fail open",
            exc,
        )
        return None


async def require_embedding_query_access(
    project_id: UUID,
    user: CurrentUser,
    role: str | None,
) -> str:
    """Require an authenticated LLM-capable member and consume one daily call."""
    require_authenticated_identity(user)
    effective_role = "admin" if role is None and user.is_superadmin else role
    if not check_llm_access(effective_role, is_anonymous=user.is_anonymous):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Paid semantic features are not available for your project role",
        )

    assert effective_role is not None  # narrowed by check_llm_access above
    redis = get_rate_limit_redis()
    if redis is not None:
        if not await check_rate_limit(redis, str(project_id), user.id, effective_role):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"Daily LLM call limit reached for your role ({effective_role}). "
                    "Try again tomorrow."
                ),
            )
    else:
        logger.warning(
            "ALERT %s: rate limiter failed open during embedding_query_rate_limit_bypass "
            "(Redis pool absent, call allowed; budget cap still enforced) "
            "— project=%s user=%s",
            FAIL_OPEN_EVENT,
            project_id,
            user.id,
            extra={
                "event": FAIL_OPEN_EVENT,
                "operation": "embedding_query_rate_limit_bypass",
                "project_id": str(project_id),
                "user_id": user.id,
            },
        )
    return effective_role
