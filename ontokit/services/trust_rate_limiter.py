"""Per-account daily submission limit for the untrusted rung (R10, R17).

Redis-backed, so the limit holds across app instances rather than being
per-process state.

This limiter deliberately fails **CLOSED**, unlike the LLM rate limiter which
fails open. That limiter meters cost; degrading it open costs money. This one is
an abuse control — degrading it open reopens exactly the hole R10 exists to
close. The trade is availability for safety, and the alert marker below makes
the degradation loud.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Protocol

from redis.exceptions import RedisError

from ontokit.core.config import settings

logger = logging.getLogger(__name__)

# Infrastructure failures only — a programming error (TypeError/AttributeError
# from a mis-wired client) must raise rather than be swallowed as "Redis down".
_REDIS_INFRA_ERRORS = (RedisError, ConnectionError, TimeoutError, OSError)

# Stable, greppable marker for the fail-closed path. Sustained volume here means
# untrusted contributors are being blocked wholesale — page on it.
UNAVAILABLE_EVENT = "trust_limiter_unavailable"

# Seconds a day-scoped key lives before Redis reclaims it. Two days of slack so
# a key written just before midnight UTC is not reclaimed mid-window.
_KEY_TTL_SECONDS = 60 * 60 * 48


class TrustLimiterRedis(Protocol):
    """The slice of an async Redis client this limiter uses."""

    async def incr(self, name: str) -> int: ...

    async def expire(self, name: str, time: int) -> bool: ...

    async def get(self, name: str) -> bytes | None: ...


def submission_key(project_id: str, user_id: str, today: str | None = None) -> str:
    """Redis key for a contributor's submissions on a project today.

    The day boundary is UTC, matching the LLM limiter and the audit
    aggregation windows.
    """
    day = today or datetime.now(UTC).date().isoformat()
    return f"trust:submit:{project_id}:{user_id}:{day}"


def _alert_unavailable(project_id: str, user_id: str, error: BaseException) -> None:
    logger.warning(
        "ALERT %s: trust submission limiter is unavailable (submission DENIED) — "
        "project=%s user=%s error=%r",
        UNAVAILABLE_EVENT,
        project_id,
        user_id,
        error,
        extra={
            "event": UNAVAILABLE_EVENT,
            "project_id": project_id,
            "user_id": user_id,
        },
    )


async def check_and_consume(
    redis: TrustLimiterRedis | None,
    project_id: str,
    user_id: str,
    limit: int | None = None,
) -> tuple[bool, int]:
    """Consume one submission from today's budget.

    Returns ``(allowed, remaining)``. ``remaining`` is the count left AFTER this
    submission, or 0 on denial.

    Fails closed: a missing client or a Redis error denies the submission and
    emits the alert marker.
    """
    daily_limit = settings.untrusted_daily_suggestion_limit if limit is None else limit
    if daily_limit <= 0:
        return (False, 0)

    if redis is None:
        _alert_unavailable(project_id, user_id, RuntimeError("no redis client configured"))
        return (False, 0)

    key = submission_key(project_id, user_id)
    try:
        used = await redis.incr(key)
        if used == 1:
            await redis.expire(key, _KEY_TTL_SECONDS)
    except _REDIS_INFRA_ERRORS as e:
        _alert_unavailable(project_id, user_id, e)
        return (False, 0)

    if used > daily_limit:
        return (False, 0)
    return (True, daily_limit - used)


async def get_remaining(
    redis: TrustLimiterRedis | None,
    project_id: str,
    user_id: str,
    limit: int | None = None,
) -> int:
    """Read today's remaining budget without consuming from it."""
    daily_limit = settings.untrusted_daily_suggestion_limit if limit is None else limit
    if redis is None:
        return 0
    try:
        raw = await redis.get(submission_key(project_id, user_id))
    except _REDIS_INFRA_ERRORS as e:
        _alert_unavailable(project_id, user_id, e)
        return 0
    used = int(raw) if raw else 0
    return max(0, daily_limit - used)
