"""Small ownership-safe Redis lock helpers for background-job admission."""

from typing import Any
from uuid import UUID

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
    redis: Any,
    key: str,
    owner: str,
    *,
    ttl_seconds: int,
) -> bool:
    """Claim ``key`` for ``owner`` until released or the safety TTL expires."""
    return bool(await redis.set(key, owner, ex=ttl_seconds, nx=True))


async def release_owned_lock(redis: Any, key: str, owner: str) -> bool:
    """Release ``key`` only when it is still held by ``owner``."""
    return bool(await redis.eval(_RELEASE_IF_OWNED, 1, key, owner))


async def renew_or_claim_owned_lock(
    redis: Any,
    key: str,
    owner: str,
    *,
    ttl_seconds: int,
) -> bool:
    """Renew the owner's lease, or reclaim it only when no owner exists."""
    return bool(await redis.eval(_RENEW_OR_CLAIM, 1, key, owner, ttl_seconds))
