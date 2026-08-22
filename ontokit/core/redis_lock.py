"""Small ownership-safe Redis lock helpers for background-job admission."""

from typing import Any

_RELEASE_IF_OWNED = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


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
