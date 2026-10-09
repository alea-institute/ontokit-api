"""Quality API endpoints — cross-references, consistency checks, duplicate detection."""

import json
import logging
import uuid
from typing import Annotated
from urllib.parse import unquote
from uuid import UUID

import pydantic
import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, status
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
    QualityJobPendingResponse,
)
from ontokit.services.cross_reference_service import get_cross_references

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_redis() -> aioredis.Redis:
    """Get the shared Redis connection pool from the application lifespan."""
    from ontokit.main import redis_pool

    if redis_pool is None:
        raise RuntimeError("Redis pool is not available")
    return redis_pool


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

    # Set pending status before enqueue to avoid race with fast-completing workers
    redis = _get_redis()
    status_key = f"quality_job_status:{project_id}:{job_id}"
    await redis.set(status_key, "pending", ex=600)

    try:
        pool = await get_arq_pool()
        job = await pool.enqueue_job(
            "run_consistency_check_task",
            str(project_id),
            resolved_branch,
            job_id,
        )
        if job is None:
            await redis.delete(status_key)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to enqueue consistency check job",
            )
    except HTTPException:
        raise
    except Exception as e:
        await redis.delete(status_key)
        logger.exception("Failed to enqueue consistency check: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to enqueue consistency check job",
        ) from e

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

    # Set pending status before enqueue to avoid race with fast-completing workers
    redis = _get_redis()
    status_key = f"duplicates_job_status:{project_id}:{job_id}"
    await redis.set(status_key, "pending", ex=600)

    try:
        pool = await get_arq_pool()
        job = await pool.enqueue_job(
            "run_duplicate_detection_task",
            str(project_id),
            resolved_branch,
            threshold,
            job_id,
        )
        if job is None:
            await redis.delete(status_key)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to enqueue duplicate detection job",
            )
    except HTTPException:
        raise
    except Exception as e:
        await redis.delete(status_key)
        logger.exception("Failed to enqueue duplicate detection: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to enqueue duplicate detection job",
        ) from e

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
    Authenticate using the bearer subprotocol or the legacy ``token`` query parameter.
    """
    from ontokit.api.utils.ws_auth import authenticate_ws
    from ontokit.api.utils.ws_forward import forward_project_events, project_reauthorizer

    if not await authenticate_ws(websocket, project_id, token):
        return

    await forward_project_events(
        websocket,
        QUALITY_UPDATES_CHANNEL,
        project_id,
        project_reauthorizer(websocket, project_id),
        pool_factory=get_arq_pool,
    )
