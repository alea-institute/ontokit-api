"""Small ownership-safe Redis lock helpers for background-job admission."""

from collections.abc import Awaitable
from typing import cast
from uuid import UUID

import redis.asyncio as aioredis

_RELEASE_IF_OWNED = """
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
    """Return the shared project-wide quality-job admission key."""
    return f"quality_job_active:{project_id}"


async def acquire_owned_lock(
    redis: aioredis.Redis,
    key: str,
    owner: str,
    *,
    ttl_seconds: int,
) -> bool:
    """Claim ``key`` for ``owner`` until released or the safety TTL expires."""
    return bool(await redis.set(key, owner, ex=ttl_seconds, nx=True))


async def release_owned_lock(redis: aioredis.Redis, key: str, owner: str) -> bool:
    """Release ``key`` only when it is still held by ``owner``."""
    result = await cast(Awaitable[str | int], redis.eval(_RELEASE_IF_OWNED, 1, key, owner))
    return bool(result)


async def renew_or_claim_owned_lock(
    redis: aioredis.Redis,
    key: str,
    owner: str,
    *,
    ttl_seconds: int,
) -> bool:
    """Renew the owner's lease, or reclaim it only when no owner exists."""
    result = await cast(
        Awaitable[str | int],
        redis.eval(_RENEW_OR_CLAIM, 1, key, owner, str(ttl_seconds)),
    )
    return bool(result)
