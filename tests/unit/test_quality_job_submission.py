"""Quality-job submission acknowledgement and reconciliation tests."""

from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi import HTTPException

from ontokit.api.routes.quality import _enqueue_claimed_quality_job
from ontokit.services.quality_job_lock import QUALITY_JOB_STATUS_TTL_SECONDS

PROJECT_ID = UUID("12345678-1234-5678-1234-567812345678")
JOB_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
STATUS_KEY = f"quality_job_status:{PROJECT_ID}:{JOB_ID}"


@pytest.mark.asyncio
async def test_acknowledgement_failure_retains_a_confirmed_live_job() -> None:
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
            redis, PROJECT_ID, JOB_ID, STATUS_KEY, "worker_task", "arg", JOB_ID
        )

    pool.enqueue_job.assert_awaited_once_with(
        "worker_task", "arg", JOB_ID, _job_id=JOB_ID
    )
    redis.set.assert_awaited_once_with(
        STATUS_KEY, "pending", ex=QUALITY_JOB_STATUS_TTL_SECONDS
    )
    redis.delete.assert_not_awaited()
    redis.eval.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirmed_absent_job_releases_claim_and_pending_status() -> None:
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
            redis, PROJECT_ID, JOB_ID, STATUS_KEY, "worker_task", JOB_ID
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
            redis, PROJECT_ID, JOB_ID, STATUS_KEY, "worker_task", JOB_ID
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
            redis, PROJECT_ID, JOB_ID, STATUS_KEY, "worker_task", JOB_ID
        )

    assert error.value.status_code == 500
    redis.delete.assert_awaited_once_with(STATUS_KEY)
    redis.eval.assert_awaited_once()
