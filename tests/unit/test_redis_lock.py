"""Tests for ownership-safe Redis admission locks."""

from unittest.mock import AsyncMock

import pytest

from ontokit.core.constants import QUALITY_JOB_TTL_SECONDS
from ontokit.core.redis_lock import acquire_owned_lock, release_owned_lock


@pytest.mark.asyncio
async def test_acquire_owned_lock_is_atomic_and_expiring() -> None:
    redis = AsyncMock()
    redis.set.return_value = True

    acquired = await acquire_owned_lock(redis, "job:key", "job-1", ttl_seconds=600)

    assert acquired is True
    redis.set.assert_awaited_once_with("job:key", "job-1", ex=600, nx=True)


def test_quality_job_ttl_exceeds_worker_timeout() -> None:
    assert QUALITY_JOB_TTL_SECONDS > 900


@pytest.mark.asyncio
async def test_release_owned_lock_uses_compare_and_delete() -> None:
    redis = AsyncMock()
    redis.eval.return_value = 1

    released = await release_owned_lock(redis, "job:key", "job-1")

    assert released is True
    script, key_count, key, owner = redis.eval.await_args.args
    assert "redis.call('get', KEYS[1]) == ARGV[1]" in script
    assert (key_count, key, owner) == (1, "job:key", "job-1")
