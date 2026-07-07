"""Redis-based daily rate limiting for LLM calls per project-user.

Per D-06 (RESEARCH.md): BYO-key calls still count against rate limit, but NOT budget.
Key format: llm:rate:{project_id}:{user_id}:{YYYY-MM-DD}
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Protocol

from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

# Infrastructure failures only — a programming error (TypeError/AttributeError
# from a mis-wired client) must raise, not silently disable metering.
_REDIS_INFRA_ERRORS = (RedisError, ConnectionError, TimeoutError, OSError)

# Per-role daily call limits. None means unlimited; 0 means no access.
# COST-03: editors 500/day, COST-04: suggesters 100/day
RATE_LIMITS: dict[str, int | None] = {
    "owner": None,  # unlimited
    "admin": None,  # unlimited
    "editor": 500,  # COST-03
    "suggester": 100,  # COST-04
    "viewer": 0,  # no access
}


class RateLimitRedis(Protocol):
    """The slice of an async Redis client the rate limiter uses."""

    async def incr(self, name: str) -> int: ...

    async def expire(self, name: str, time: int, nx: bool = ...) -> bool: ...

    async def get(self, name: str) -> bytes | None: ...


def _rate_key(project_id: str, user_id: str, today: str | None = None) -> str:
    """Build the Redis key for today's call count.

    The day boundary is UTC so the rate window lines up with the UTC-based
    budget/audit aggregation windows.
    """
    day = today or datetime.now(UTC).date().isoformat()
    return f"llm:rate:{project_id}:{user_id}:{day}"


async def check_rate_limit(
    redis: RateLimitRedis,
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
        current: int = await redis.incr(key)
        # NX: set the 24h TTL only when the key has none. Unlike the
        # `current == 1` guard, this also repairs keys left TTL-less by a
        # crash between INCR and EXPIRE (which would otherwise rate-limit
        # the user forever once the counter crossed the cap).
        await redis.expire(key, 86400, nx=True)
        return current <= limit
    except _REDIS_INFRA_ERRORS:
        logger.warning(
            "Redis error checking rate limit for user %s in project %s — allowing call",
            user_id,
            project_id,
        )
        # Fail open: if Redis is unavailable, don't block legitimate users
        return True


async def get_remaining_calls(
    redis: RateLimitRedis,
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
        raw = await redis.get(key)
        if raw is None:
            return limit
        current = int(raw)
        return max(0, limit - current)
    except _REDIS_INFRA_ERRORS:
        logger.warning(
            "Redis error fetching remaining calls for user %s in project %s",
            user_id,
            project_id,
        )
        return limit
