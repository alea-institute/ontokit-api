"""Shared ARQ Redis pool for background job enqueueing."""

from arq import ArqRedis, create_pool

from ontokit.core.redis import get_redis_settings

_arq_pool: ArqRedis | None = None


async def get_arq_pool() -> ArqRedis:
    """Get or create the ARQ Redis connection pool.

    Uses the shared ``get_redis_settings()`` rather than
    ``RedisSettings.from_dsn``: the worker's version carries the DSN's
    credentials (URL-decoded) and the ``rediss://`` TLS flag across, which
    ``from_dsn`` drops. Both processes talk to the same Redis, so having the API
    side fail to authenticate where the worker succeeds — the exact shape of the
    bug fixed in the worker on the FOLIO DEV deploy — is a difference with no
    justification.
    """
    global _arq_pool
    if _arq_pool is None:
        _arq_pool = await create_pool(get_redis_settings())
    return _arq_pool


async def close_arq_pool() -> None:
    """Close the cached ARQ Redis pool."""
    global _arq_pool
    if _arq_pool is not None:
        await _arq_pool.close()
        _arq_pool = None
