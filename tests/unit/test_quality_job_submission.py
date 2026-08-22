"""Quality-job enqueue acknowledgement and reconciliation tests."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from ontokit.api.routes.quality import _enqueue_claimed_quality_job

LOCK_KEY = "quality_job_active:project-1"
STATUS_KEY = "quality_job_status:project-1:job-1"
JOB_ID = "job-1"


@pytest.mark.asyncio
async def test_acknowledgement_failure_retains_confirmed_live_job() -> None:
    redis = AsyncMock()
    pool = AsyncMock()
    pool.enqueue_job.side_effect = RuntimeError("connection dropped after write")

    with (
        patch("ontokit.api.routes.quality.get_arq_pool", new=AsyncMock(return_value=pool)),
        patch(
            "ontokit.api.routes.quality._enqueued_job_exists",
            new=AsyncMock(return_value=True),
        ),
    ):
        await _enqueue_claimed_quality_job(
            redis,
            LOCK_KEY,
            STATUS_KEY,
            JOB_ID,
            "worker_task",
            "arg",
            JOB_ID,
        )

    pool.enqueue_job.assert_awaited_once_with(
        "worker_task",
        "arg",
        JOB_ID,
        _job_id=JOB_ID,
    )
    redis.delete.assert_not_awaited()
    redis.eval.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirmed_absent_job_releases_claim_and_status() -> None:
    redis = AsyncMock()
    pool = AsyncMock()
    pool.enqueue_job.return_value = None

    with (
        patch("ontokit.api.routes.quality.get_arq_pool", new=AsyncMock(return_value=pool)),
        patch(
            "ontokit.api.routes.quality._enqueued_job_exists",
            new=AsyncMock(return_value=False),
        ),
        pytest.raises(HTTPException) as error,
    ):
        await _enqueue_claimed_quality_job(
            redis,
            LOCK_KEY,
            STATUS_KEY,
            JOB_ID,
            "worker_task",
            JOB_ID,
        )

    assert error.value.status_code == 500
    redis.delete.assert_awaited_once_with(STATUS_KEY)
    redis.eval.assert_awaited_once()


@pytest.mark.asyncio
async def test_unknown_submission_state_fails_closed_and_retains_claim() -> None:
    redis = AsyncMock()
    pool = AsyncMock()
    pool.enqueue_job.side_effect = RuntimeError("connection dropped")

    with (
        patch("ontokit.api.routes.quality.get_arq_pool", new=AsyncMock(return_value=pool)),
        patch(
            "ontokit.api.routes.quality._enqueued_job_exists",
            new=AsyncMock(return_value=None),
        ),
        pytest.raises(HTTPException) as error,
    ):
        await _enqueue_claimed_quality_job(
            redis,
            LOCK_KEY,
            STATUS_KEY,
            JOB_ID,
            "worker_task",
            JOB_ID,
        )

    assert error.value.status_code == 503
    redis.delete.assert_not_awaited()
    redis.eval.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_status_failure_attempts_claim_cleanup() -> None:
    redis = AsyncMock()
    redis.set.side_effect = RuntimeError("Redis unavailable")

    with pytest.raises(HTTPException) as error:
        await _enqueue_claimed_quality_job(
            redis,
            LOCK_KEY,
            STATUS_KEY,
            JOB_ID,
            "worker_task",
            JOB_ID,
        )

    assert error.value.status_code == 500
    redis.delete.assert_awaited_once_with(STATUS_KEY)
    redis.eval.assert_awaited_once()
