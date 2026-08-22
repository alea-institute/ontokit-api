"""Atomic admission and ownership-safe release for project quality jobs."""

from unittest.mock import AsyncMock

import pytest

from ontokit.services.quality_job_lock import (
    QUALITY_JOB_LOCK_TTL_SECONDS,
    claim_quality_job,
    quality_job_lock_key,
    release_quality_job,
    renew_or_claim_quality_job,
)


@pytest.mark.asyncio
async def test_claim_is_atomic_and_crash_safe() -> None:
    redis = AsyncMock()
    redis.set.return_value = True

    assert await claim_quality_job(redis, "project-1", "job-1") is True
    redis.set.assert_awaited_once_with(
        quality_job_lock_key("project-1"),
        "job-1",
        nx=True,
        ex=QUALITY_JOB_LOCK_TTL_SECONDS,
    )


@pytest.mark.asyncio
async def test_failed_claim_is_reported() -> None:
    redis = AsyncMock()
    redis.set.return_value = False

    assert await claim_quality_job(redis, "project-1", "job-2") is False


@pytest.mark.asyncio
async def test_release_uses_compare_and_delete() -> None:
    redis = AsyncMock()

    await release_quality_job(redis, "project-1", "job-1")

    args = redis.eval.await_args.args
    assert "redis.call('get', KEYS[1]) == ARGV[1]" in args[0]
    assert args[1:] == (1, quality_job_lock_key("project-1"), "job-1")


@pytest.mark.asyncio
async def test_worker_renews_or_reclaims_its_lease_atomically() -> None:
    redis = AsyncMock()
    redis.eval.return_value = 1

    assert await renew_or_claim_quality_job(redis, "project-1", "job-1") is True

    args = redis.eval.await_args.args
    assert "not owner or owner == ARGV[1]" in args[0]
    assert args[1:] == (
        1,
        quality_job_lock_key("project-1"),
        "job-1",
        str(QUALITY_JOB_LOCK_TTL_SECONDS),
    )


@pytest.mark.asyncio
async def test_worker_refuses_a_newer_jobs_lease() -> None:
    redis = AsyncMock()
    redis.eval.return_value = 0

    assert await renew_or_claim_quality_job(redis, "project-1", "stale-job") is False
