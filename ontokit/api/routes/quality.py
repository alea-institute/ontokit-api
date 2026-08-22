"""Quality API endpoints — cross-references, consistency checks, duplicate detection."""

import json
import logging
import uuid
from typing import Annotated
from urllib.parse import unquote
from uuid import UUID

import pydantic
import redis.asyncio as aioredis
from arq import ArqRedis
from arq.jobs import Job, JobStatus
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from ontokit.api.dependencies import load_project_graph, resolve_branch, verify_project_access
from ontokit.api.utils.redis import get_arq_pool
from ontokit.core.auth import OptionalUser, RequiredUser
from ontokit.core.constants import QUALITY_UPDATES_CHANNEL
from ontokit.core.database import get_db
from ontokit.schemas.quality import (
    ConsistencyCheckResult,
    ConsistencyCheckTriggerResponse,
    CrossReferencesResponse,
    DuplicateDetectionResult,
    DuplicateDetectionTriggerResponse,
    QualityJobConflictDetail,
    QualityJobConflictResponse,
    QualityJobPendingResponse,
)
from ontokit.services.cross_reference_service import get_cross_references
from ontokit.services.quality_job_lock import (
    QUALITY_JOB_STATUS_TTL_SECONDS,
    claim_quality_job,
    release_quality_job,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_QUALITY_JOB_CONFLICT = QualityJobConflictDetail(
    message="A quality job is already active for this project"
).model_dump()


def _get_redis() -> aioredis.Redis:
    """Get the shared Redis connection pool from the application lifespan."""
    from ontokit.main import redis_pool

    if redis_pool is None:
        raise RuntimeError("Redis pool is not available")
    return redis_pool


async def _cleanup_failed_submission(
    redis: aioredis.Redis,
    project_id: UUID,
    job_id: str,
    status_key: str,
) -> None:
    try:
        await redis.delete(status_key)
    except Exception:
        logger.exception("Could not delete failed quality-job status %s", job_id)
    try:
        await release_quality_job(redis, project_id, job_id)
    except Exception:
        # The lease TTL remains the crash-recovery boundary when Redis is down.
        logger.exception("Could not release failed quality-job claim %s", job_id)


async def _enqueued_job_exists(pool: ArqRedis, job_id: str) -> bool | None:
    """Return queue truth after an ambiguous enqueue acknowledgement."""
    try:
        return await Job(job_id, redis=pool).status() != JobStatus.not_found
    except Exception:
        logger.exception("Could not reconcile quality job submission %s", job_id)
        return None


async def _enqueue_claimed_quality_job(
    redis: aioredis.Redis,
    project_id: UUID,
    job_id: str,
    status_key: str,
    function: str,
    *args: object,
) -> None:
    """Enqueue with a deterministic ID and preserve ambiguous live claims."""
    try:
        await redis.set(
            status_key,
            "pending",
            ex=QUALITY_JOB_STATUS_TTL_SECONDS,
        )
    except Exception as exc:
        await _cleanup_failed_submission(redis, project_id, job_id, status_key)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to initialize quality job",
        ) from exc
    try:
        pool = await get_arq_pool()
    except Exception as exc:
        await _cleanup_failed_submission(redis, project_id, job_id, status_key)
        logger.exception("Failed to acquire ARQ pool for %s", function)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to enqueue quality job",
        ) from exc

    enqueue_error: Exception | None = None
    try:
        job = await pool.enqueue_job(function, *args, _job_id=job_id)
    except Exception as exc:
        enqueue_error = exc
        job = None

    if job is not None:
        return

    exists = await _enqueued_job_exists(pool, job_id)
    if exists is True:
        if enqueue_error is not None:
            logger.warning(
                "Quality job %s exists after enqueue acknowledgement failed", job_id
            )
        return
    if exists is None:
        # Fail closed: the TTL-backed claim prevents a duplicate while an
        # operator or retry determines whether the deterministic job exists.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Quality job submission status is temporarily unknown",
        ) from enqueue_error

    await _cleanup_failed_submission(redis, project_id, job_id, status_key)
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Failed to enqueue quality job",
    ) from enqueue_error


@router.get(
    "/{project_id}/entities/{iri:path}/references",
    response_model=CrossReferencesResponse,
)
async def get_entity_references(
    project_id: UUID,
    iri: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: OptionalUser,
    branch: str | None = Query(default=None, description="Branch name"),
) -> CrossReferencesResponse:
    """Get all cross-references to a specific entity."""
    await verify_project_access(project_id, db, user)
    decoded_iri = unquote(iri)
    graph, _ = await load_project_graph(project_id, branch, db)
    return get_cross_references(graph, decoded_iri)


@router.post(
    "/{project_id}/quality/check",
    response_model=ConsistencyCheckTriggerResponse,
    responses={409: {"model": QualityJobConflictResponse}},
)
async def trigger_consistency_check(
    project_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: RequiredUser,
    branch: str | None = Query(default=None, description="Branch name"),
) -> ConsistencyCheckTriggerResponse:
    """Enqueue a consistency check as a background job."""
    await verify_project_access(project_id, db, user)
    resolved_branch = await resolve_branch(project_id, branch)

    job_id = str(uuid.uuid4())

    redis = _get_redis()
    if not await claim_quality_job(redis, project_id, job_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_QUALITY_JOB_CONFLICT,
        )

    # Set pending status before enqueue to avoid race with fast-completing workers
    status_key = f"quality_job_status:{project_id}:{job_id}"

    await _enqueue_claimed_quality_job(
        redis,
        project_id,
        job_id,
        status_key,
        "run_consistency_check_task",
        str(project_id),
        resolved_branch,
        job_id,
    )

    return ConsistencyCheckTriggerResponse(job_id=job_id)


@router.get(
    "/{project_id}/quality/jobs/{job_id}",
    response_model=ConsistencyCheckResult,
    responses={202: {"model": QualityJobPendingResponse, "description": "Job is still pending"}},
)
async def get_quality_job_result(
    project_id: UUID,
    job_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: OptionalUser,
) -> ConsistencyCheckResult | Response:
    """Get consistency check results by job ID.

    Returns 200 with results when complete, 202 when still pending,
    or 404 when the job ID is unknown or expired.
    """
    await verify_project_access(project_id, db, user)

    redis = _get_redis()
    cached = await redis.get(f"quality_job:{project_id}:{job_id}")
    if cached is not None:
        try:
            return ConsistencyCheckResult.model_validate_json(cached)
        except pydantic.ValidationError as e:
            logger.error("Corrupt cached consistency result for job %s: %s", job_id, e)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Cached result is corrupt",
            ) from e

    # No result yet — check if the job is still pending or failed
    job_status = await redis.get(f"quality_job_status:{project_id}:{job_id}")
    if job_status is not None:
        status_str = job_status.decode() if isinstance(job_status, bytes) else str(job_status)
        if status_str != "pending":
            try:
                failure = json.loads(status_str)
                error_msg = failure.get("error", "Unknown error")
            except json.JSONDecodeError:
                error_msg = "Unknown error"
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Job failed: {error_msg}",
            )
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content=QualityJobPendingResponse(job_id=job_id).model_dump(),
        )

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Job result not found or expired",
    )


@router.get(
    "/{project_id}/quality/issues",
    response_model=ConsistencyCheckResult,
)
async def get_consistency_issues(
    project_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: OptionalUser,
    branch: str | None = Query(default=None, description="Branch name"),
) -> ConsistencyCheckResult:
    """Get cached consistency check results."""
    await verify_project_access(project_id, db, user)

    # Resolve the branch so cache keys match what trigger_consistency_check stored
    resolved_branch = await resolve_branch(project_id, branch)

    redis = _get_redis()
    cached = await redis.get(f"quality:{project_id}:{resolved_branch}")
    if cached is not None:
        try:
            return ConsistencyCheckResult.model_validate_json(cached)
        except pydantic.ValidationError as e:
            logger.error("Corrupt cached consistency result: %s", e)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Cached result is corrupt",
            ) from e

    # No cached result — return empty
    from datetime import UTC, datetime

    return ConsistencyCheckResult(
        project_id=str(project_id),
        branch=resolved_branch,
        issues=[],
        checked_at=datetime.now(UTC).isoformat(),
        duration_ms=0,
    )


@router.post(
    "/{project_id}/quality/duplicates",
    response_model=DuplicateDetectionTriggerResponse,
    responses={409: {"model": QualityJobConflictResponse}},
)
async def detect_duplicates(
    project_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: RequiredUser,
    branch: str | None = Query(default=None, description="Branch name"),
    threshold: float = Query(default=0.85, ge=0.5, le=1.0),
) -> DuplicateDetectionTriggerResponse:
    """Enqueue duplicate detection as a background job."""
    await verify_project_access(project_id, db, user)
    resolved_branch = await resolve_branch(project_id, branch)

    job_id = str(uuid.uuid4())

    redis = _get_redis()
    if not await claim_quality_job(redis, project_id, job_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_QUALITY_JOB_CONFLICT,
        )

    # Set pending status before enqueue to avoid race with fast-completing workers
    status_key = f"duplicates_job_status:{project_id}:{job_id}"

    await _enqueue_claimed_quality_job(
        redis,
        project_id,
        job_id,
        status_key,
        "run_duplicate_detection_task",
        str(project_id),
        resolved_branch,
        threshold,
        job_id,
    )

    return DuplicateDetectionTriggerResponse(job_id=job_id)


@router.get(
    "/{project_id}/quality/duplicates/jobs/{job_id}",
    response_model=DuplicateDetectionResult,
    responses={202: {"model": QualityJobPendingResponse, "description": "Job is still pending"}},
)
async def get_duplicate_job_result(
    project_id: UUID,
    job_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: OptionalUser,
) -> DuplicateDetectionResult | Response:
    """Get duplicate detection results by job ID.

    Returns 200 with results when complete, 202 when still pending,
    or 404 when the job ID is unknown or expired.
    """
    await verify_project_access(project_id, db, user)

    redis = _get_redis()
    cached = await redis.get(f"duplicates_job:{project_id}:{job_id}")
    if cached is not None:
        try:
            return DuplicateDetectionResult.model_validate_json(cached)
        except pydantic.ValidationError as e:
            logger.error("Corrupt cached duplicates result for job %s: %s", job_id, e)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Cached result is corrupt",
            ) from e

    # No result yet — check if the job is still pending or failed
    job_status = await redis.get(f"duplicates_job_status:{project_id}:{job_id}")
    if job_status is not None:
        status_str = job_status.decode() if isinstance(job_status, bytes) else str(job_status)
        if status_str != "pending":
            try:
                failure = json.loads(status_str)
                error_msg = failure.get("error", "Unknown error")
            except json.JSONDecodeError:
                error_msg = "Unknown error"
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Job failed: {error_msg}",
            )
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content=QualityJobPendingResponse(job_id=job_id).model_dump(),
        )

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Job result not found or expired",
    )


@router.get(
    "/{project_id}/quality/duplicates/latest",
    response_model=DuplicateDetectionResult,
)
async def get_latest_duplicates(
    project_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: OptionalUser,
    branch: str | None = Query(default=None, description="Branch name"),
) -> DuplicateDetectionResult:
    """Get cached duplicate detection results."""
    await verify_project_access(project_id, db, user)
    resolved_branch = await resolve_branch(project_id, branch)

    redis = _get_redis()
    cached = await redis.get(f"duplicates:{project_id}:{resolved_branch}")
    if cached is not None:
        try:
            return DuplicateDetectionResult.model_validate_json(cached)
        except pydantic.ValidationError as e:
            logger.error("Corrupt cached duplicates result: %s", e)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Cached result is corrupt",
            ) from e

    from datetime import UTC, datetime

    return DuplicateDetectionResult(
        clusters=[],
        threshold=0.85,
        checked_at=datetime.now(UTC).isoformat(),
    )


@router.websocket("/{project_id}/quality/ws")
async def quality_websocket(
    websocket: WebSocket,
    project_id: UUID,
    token: str | None = Query(default=None, description="Bearer token for authentication"),
) -> None:
    """
    WebSocket endpoint for real-time quality check updates.

    Connect to receive notifications when:
    - Consistency check starts / completes / fails
    - Duplicate detection starts / completes / fails

    Messages are JSON objects with a "type" field indicating the event type.
    Pass ``token`` as a query parameter for authentication.
    """
    import asyncio
    import contextlib

    from ontokit.api.utils.ws_auth import authenticate_ws

    project_id_str = str(project_id)

    if not await authenticate_ws(websocket, project_id, token):
        return

    pubsub = None
    try:
        pool = await get_arq_pool()
        pubsub = pool.pubsub()
        await pubsub.subscribe(QUALITY_UPDATES_CHANNEL)

        # TODO: Replace this 0.1s poll loop with concurrent coroutines
        # (blocking Redis listener + blocking WS receive) when tackling #78
        # (per-project Redis pubsub channels). The lint WS uses the same
        # pattern and both should be refactored together.
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
            if message and message["type"] == "message":
                try:
                    data = json.loads(message["data"])
                    if data.get("project_id") == project_id_str:
                        await websocket.send_json(data)
                except json.JSONDecodeError:
                    pass

            # Check for WebSocket messages (keepalive/close)
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=0.1)
            except TimeoutError:
                pass
            except WebSocketDisconnect:
                break

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error("Quality WebSocket error for project %s: %s", project_id, e)
    finally:
        if pubsub:
            with contextlib.suppress(Exception):
                await pubsub.unsubscribe(QUALITY_UPDATES_CHANNEL)
            with contextlib.suppress(Exception):
                await pubsub.aclose()  # type: ignore[no-untyped-call]
