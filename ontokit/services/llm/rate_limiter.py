"""Redis-based daily rate limiting for LLM calls per project-user.

Per D-06 (RESEARCH.md): BYO-key calls still count against rate limit, but NOT budget.
Key format: llm:rate:{project_id}:{user_id}:{YYYY-MM-DD}
"""

from __future__ import annotations

import logging
from datetime import date

logger = logging.getLogger(__name__)

# Per-role daily call limits. None means unlimited; 0 means no access.
# COST-03: editors 500/day, COST-04: suggesters 100/day
RATE_LIMITS: dict[str, int | None] = {
    "owner": None,      # unlimited
    "admin": None,      # unlimited
    "editor": 500,      # COST-03
    "suggester": 100,   # COST-04
    "viewer": 0,        # no access
}


def _rate_key(project_id: str, user_id: str, today: str | None = None) -> str:
    """Build the Redis key for today's call count."""
    day = today or date.today().isoformat()
    return f"llm:rate:{project_id}:{user_id}:{day}"


async def check_rate_limit(
    redis: object,
    project_id: str,
    user_id: str,
    role: str,
) -> bool:
    """Check whether the user is within their daily rate limit.

    Uses INCR + EXPIRE(86400) so the counter auto-expires at end of day.

    Args:
        redis: An async Redis client (e.g. redis.asyncio.Redis).
        project_id: The project UUID string.
        user_id: The authenticated user ID.
        role: The user's role in the project.

    Returns:
        True if the call is within the limit; False if exceeded or blocked.
    """
    limit = RATE_LIMITS.get(role, 0)

    # Viewer or unknown role: always blocked
    if limit == 0:
        return False

    # Owner / admin: unlimited
    if limit is None:
        return True

    key = _rate_key(project_id, user_id)
    try:
        # Atomically increment; set 24-hour TTL on first write
        current: int = await redis.incr(key)  # type: ignore[attr-defined]
        if current == 1:
            # First call today — set TTL to 24 hours
            await redis.expire(key, 86400)  # type: ignore[attr-defined]
        return current <= limit
    except Exception:
        logger.warning(
            "Redis error checking rate limit for user %s in project %s — allowing call",
            user_id,
            project_id,
        )
        # Fail open: if Redis is unavailable, don't block legitimate users
        return True


async def get_remaining_calls(
    redis: object,
    project_id: str,
    user_id: str,
    role: str,
) -> int | None:
    """Return the number of remaining calls for today.

    Returns:
        None for unlimited (owner/admin).
        0 for blocked roles (viewer).
        Remaining count (clamped to 0) for editor/suggester.
    """
    limit = RATE_LIMITS.get(role, 0)

    if limit == 0:
        return 0

    if limit is None:
        return None

    key = _rate_key(project_id, user_id)
    try:
        raw = await redis.get(key)  # type: ignore[attr-defined]
        if raw is None:
            return limit
        current = int(raw)
        return max(0, limit - current)
    except Exception:
        logger.warning(
            "Redis error fetching remaining calls for user %s in project %s",
            user_id,
            project_id,
        )
        return limit
