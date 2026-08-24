"""Redis-based daily rate limiting for LLM calls per project-user.

Per D-06 (RESEARCH.md): BYO-key calls still count against rate limit, but NOT budget.
Key format: llm:rate:{project_id}:{user_id}:{YYYY-MM-DD}
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

# Infrastructure failures only — a programming error (TypeError/AttributeError
# from a mis-wired client) must raise, not silently disable metering.
_REDIS_INFRA_ERRORS = (RedisError, ConnectionError, TimeoutError, OSError)

# Stable, greppable event name for the fail-open path. Every place the rate
# limiter degrades to "allow" because Redis is unavailable emits this exact
# marker (message prefix + `event` extra), so ops can alert on ONE signal:
#   logger name "ontokit.services.llm.rate_limiter", event=llm_rate_limiter_fail_open
# Sustained volume here means metering is silently disabled — page on it.
FAIL_OPEN_EVENT = "llm_rate_limiter_fail_open"


def _alert_fail_open(operation: str, project_id: str, user_id: str, error: BaseException) -> None:
    """Emit the single actionable fail-open alert shared by every degraded path."""
    logger.warning(
        "ALERT %s: rate limiter failed open during %s (metering disabled, call allowed) "
        "— project=%s user=%s error=%r",
        FAIL_OPEN_EVENT,
        operation,
        project_id,
        user_id,
        error,
        extra={
            "event": FAIL_OPEN_EVENT,
            "operation": operation,
            "project_id": project_id,
            "user_id": user_id,
        },
    )


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

    async def eval(self, script: str, numkeys: int, *keys_and_args: object) -> int: ...


@dataclass(frozen=True)
class RateLimitReservation:
    """Outcome of an idempotent multi-unit rate reservation.

    ``acquired`` is true only when this attempt incremented the counter.  A
    caller may therefore compensate a failed downstream operation without
    refunding a reservation owned by an earlier successful attempt.
    """

    accepted: bool
    acquired: bool


def _rate_key(project_id: str, user_id: str, today: str | None = None) -> str:
    """Build the Redis key for today's call count.

    The day boundary is UTC so the rate window lines up with the UTC-based
    budget/audit aggregation windows.
    """
    day = today or datetime.now(UTC).date().isoformat()
    return f"llm:rate:{project_id}:{user_id}:{day}"


_CONSUME_UNITS_SCRIPT = """
local receipt_enabled = ARGV[4] == '1'
if receipt_enabled and redis.call('EXISTS', KEYS[2]) == 1 then
  if tonumber(redis.call('GET', KEYS[2])) ~= tonumber(ARGV[1]) then
    return -1
  end
  return 1
end
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
local units = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
if current + units > limit then
  return 0
end
redis.call('INCRBY', KEYS[1], units)
if redis.call('TTL', KEYS[1]) < 0 then
  redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3]))
end
if receipt_enabled then
  redis.call('SET', KEYS[2], ARGV[1], 'EX', tonumber(ARGV[3]))
end
return 2
"""

_RELEASE_UNITS_SCRIPT = """
local reserved = tonumber(redis.call('GET', KEYS[2]))
if reserved == nil then
  return 0
end
if reserved ~= tonumber(ARGV[1]) then
  return -1
end
redis.call('DEL', KEYS[2])
local remaining = redis.call('DECRBY', KEYS[1], reserved)
if remaining <= 0 then
  redis.call('DEL', KEYS[1])
end
return 1
"""


async def reserve_rate_limit_units(
    redis: RateLimitRedis,
    project_id: str,
    user_id: str,
    role: str,
    units: int,
    *,
    reservation_id: str | None = None,
) -> RateLimitReservation:
    """Reserve units and report whether this attempt owns the increment."""
    if units <= 0:
        raise ValueError("rate-limit units must be positive")
    limit = RATE_LIMITS.get(role, 0)
    if limit == 0:
        return RateLimitReservation(accepted=False, acquired=False)
    if limit is None:
        return RateLimitReservation(accepted=True, acquired=False)

    key = _rate_key(project_id, user_id)
    receipt_key = f"{key}:reservation:{reservation_id or 'none'}"
    try:
        result = await redis.eval(
            _CONSUME_UNITS_SCRIPT,
            2,
            key,
            receipt_key,
            units,
            limit,
            86400,
            1 if reservation_id else 0,
        )
        return RateLimitReservation(accepted=result > 0, acquired=result == 2)
    except _REDIS_INFRA_ERRORS as exc:
        _alert_fail_open("reserve_rate_limit_units", project_id, user_id, exc)
        return RateLimitReservation(accepted=True, acquired=False)


async def consume_rate_limit_units(
    redis: RateLimitRedis,
    project_id: str,
    user_id: str,
    role: str,
    units: int,
    *,
    reservation_id: str | None = None,
) -> bool:
    """Atomically reserve multiple provider-call units without partial burns.

    ``reservation_id`` makes a retry of the same logical fan-out idempotent for
    the current UTC rate window.
    """
    reservation = await reserve_rate_limit_units(
        redis,
        project_id,
        user_id,
        role,
        units,
        reservation_id=reservation_id,
    )
    return reservation.accepted


async def release_rate_limit_units(
    redis: RateLimitRedis,
    project_id: str,
    user_id: str,
    role: str,
    units: int,
    *,
    reservation_id: str,
) -> bool:
    """Atomically refund a reservation acquired by a failed downstream action.

    The receipt stores the reserved unit count, and the Lua script deletes it
    together with the decrement. Repeated releases are therefore harmless.
    """
    if units <= 0:
        raise ValueError("rate-limit units must be positive")
    if not reservation_id:
        raise ValueError("reservation_id is required to release rate-limit units")
    limit = RATE_LIMITS.get(role, 0)
    if limit is None:
        return False
    if limit == 0:
        return False

    key = _rate_key(project_id, user_id)
    receipt_key = f"{key}:reservation:{reservation_id}"
    try:
        released = await redis.eval(
            _RELEASE_UNITS_SCRIPT,
            2,
            key,
            receipt_key,
            units,
        )
        if released < 0:
            logger.error(
                "rate-limit reservation unit mismatch during release — project=%s user=%s",
                project_id,
                user_id,
            )
            return False
        return bool(released)
    except _REDIS_INFRA_ERRORS as exc:
        _alert_fail_open("release_rate_limit_units", project_id, user_id, exc)
        return False


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
    except _REDIS_INFRA_ERRORS as exc:
        # Fail open: if Redis is unavailable, don't block legitimate users.
        # The DB budget layer remains a non-fail-open backstop against runaway spend.
        _alert_fail_open("check_rate_limit", project_id, user_id, exc)
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
    except _REDIS_INFRA_ERRORS as exc:
        # Fail open: report the full allowance rather than block on infra error.
        _alert_fail_open("get_remaining_calls", project_id, user_id, exc)
        return limit
