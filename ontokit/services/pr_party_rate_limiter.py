"""Per-reviewer daily cap on PR Party writes (KTD16, R10).

This is *runaway-loop* protection, not throttling. PR Party has a handful of
config-provisioned reviewers (KTD12); no human approaches the default cap. What
the cap actually stops is a client retry loop — the exact shape that turns one
tap into a hundred GitHub calls — and it stops it per account rather than per
IP, because two reviewers behind one office NAT must not share a budget.

Modelled on :mod:`ontokit.services.trust_rate_limiter`, with one deliberate
difference: **whether an unavailable limiter denies is the caller's decision,
not the limiter's.** The trust limiter always fails closed because it guards an
abuse surface open to the internet. Here the two call sites want opposite
things:

- **Actuation fails CLOSED.** A verdict is irreversible and travels to GitHub;
  if the counter cannot be trusted, the honest answer is 503 and a retry, not an
  unmetered write path.
- **Credential submission fails OPEN.** Failing closed there would lock a
  reviewer out of connecting the very PAT that un-degrades them (U2's own
  reasoning in ``save_credential``), for the sake of metering a route that
  cannot touch GitHub state at all. The global per-IP limit still applies.

So the limiter reports three distinct outcomes and lets each route choose.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from redis.exceptions import RedisError

from ontokit.core.config import settings

logger = logging.getLogger(__name__)

__all__ = [
    "UNAVAILABLE_EVENT",
    "ActionLimiterRedis",
    "LimiterOutcome",
    "action_key",
    "check_and_consume",
]

# Infrastructure failures only — a mis-wired client raising TypeError must
# surface as the programming error it is, not be laundered into "Redis down".
_REDIS_INFRA_ERRORS = (RedisError, ConnectionError, TimeoutError, OSError)

#: Stable, greppable marker. Sustained volume here means reviewers are being
#: refused actuation wholesale — page on it.
UNAVAILABLE_EVENT = "pr_party_limiter_unavailable"

#: Two days of slack so a key written just before midnight UTC is not reclaimed
#: mid-window.
_KEY_TTL_SECONDS = 60 * 60 * 48


class ActionLimiterRedis(Protocol):
    """The slice of an async Redis client this limiter uses."""

    async def incr(self, name: str) -> int: ...

    async def expire(self, name: str, time: int) -> bool: ...


class LimiterOutcome(StrEnum):
    """Why a write was or was not permitted.

    ``OVER_LIMIT`` and ``UNAVAILABLE`` are separate because they mean opposite
    things to a caller: the first is the reviewer's own budget (429, come back
    tomorrow), the second is our infrastructure (503, retry shortly).
    """

    ALLOWED = "allowed"
    OVER_LIMIT = "over_limit"
    UNAVAILABLE = "unavailable"


def action_key(user_id: str, today: str | None = None) -> str:
    """Redis key for one reviewer's PR Party writes today.

    The day boundary is UTC, matching every other limiter and audit window in
    the codebase.
    """
    day = today or datetime.now(UTC).date().isoformat()
    return f"pr_party:actions:{user_id}:{day}"


def _alert_unavailable(user_id: str, error: BaseException) -> None:
    logger.warning(
        "ALERT %s: the PR Party action limiter is unavailable — user=%s error=%r",
        UNAVAILABLE_EVENT,
        user_id,
        error,
        extra={"event": UNAVAILABLE_EVENT, "user_id": user_id},
    )


async def check_and_consume(
    redis: ActionLimiterRedis | None,
    user_id: str,
    *,
    limit: int | None = None,
) -> tuple[LimiterOutcome, int]:
    """Consume one write from today's budget.

    Returns ``(outcome, remaining)``, where ``remaining`` is the count left
    AFTER this write and is 0 unless the outcome is
    :attr:`LimiterOutcome.ALLOWED`.
    """
    daily_limit = settings.pr_party_daily_action_limit if limit is None else limit
    if daily_limit <= 0:
        return (LimiterOutcome.OVER_LIMIT, 0)

    if redis is None:
        _alert_unavailable(user_id, RuntimeError("no redis client configured"))
        return (LimiterOutcome.UNAVAILABLE, 0)

    key = action_key(user_id)
    try:
        used = await redis.incr(key)
        if used == 1:
            await redis.expire(key, _KEY_TTL_SECONDS)
    except _REDIS_INFRA_ERRORS as e:
        _alert_unavailable(user_id, e)
        return (LimiterOutcome.UNAVAILABLE, 0)

    if used > daily_limit:
        return (LimiterOutcome.OVER_LIMIT, 0)
    return (LimiterOutcome.ALLOWED, daily_limit - used)
