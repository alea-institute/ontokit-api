"""Project-level admission control for expensive quality jobs."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import cast
from uuid import UUID

import redis.asyncio as aioredis

QUALITY_JOB_LOCK_TTL_SECONDS = 30 * 60
QUALITY_JOB_STATUS_TTL_SECONDS = QUALITY_JOB_LOCK_TTL_SECONDS

_RELEASE_IF_OWNER = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""

_RENEW_OR_CLAIM = """
local owner = redis.call('get', KEYS[1])
if not owner or owner == ARGV[1] then
  redis.call('set', KEYS[1], ARGV[1], 'EX', ARGV[2])
  return 1
end
return 0
"""


def quality_job_lock_key(project_id: UUID | str) -> str:
    """One shared slot across consistency and duplicate jobs for a project."""
    return f"quality_job_active:{project_id}"


async def claim_quality_job(
    redis: aioredis.Redis, project_id: UUID | str, job_id: str
) -> bool:
    """Atomically claim the project's quality-job slot with a crash-safe TTL."""
    claimed = await redis.set(
        quality_job_lock_key(project_id),
        job_id,
        nx=True,
        ex=QUALITY_JOB_LOCK_TTL_SECONDS,
    )
    return bool(claimed)


async def renew_or_claim_quality_job(
    redis: aioredis.Redis, project_id: UUID | str, job_id: str
) -> bool:
    """Renew this job's lease, or reclaim it after an idle expiry.

    A delayed/retried job is refused when a newer job owns the project slot.
    """
    renewed = await cast(
        Awaitable[str | int],
        redis.eval(
            _RENEW_OR_CLAIM,
            1,
            quality_job_lock_key(project_id),
            job_id,
            str(QUALITY_JOB_LOCK_TTL_SECONDS),
        ),
    )
    return bool(renewed)


async def release_quality_job(
    redis: aioredis.Redis, project_id: UUID | str, job_id: str
) -> None:
    """Release only the caller's claim, never a replacement claim after expiry."""
    await cast(
        Awaitable[str | int],
        redis.eval(_RELEASE_IF_OWNER, 1, quality_job_lock_key(project_id), job_id),
    )
