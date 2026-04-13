"""Quality API endpoints — cross-references, consistency checks, duplicate detection."""

import json
import logging
import uuid
from typing import Annotated
from urllib.parse import unquote
from uuid import UUID

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from sqlalchemy.ext.asyncio import AsyncSession

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

    try:
        pool = await get_arq_pool()
        job = await pool.enqueue_job(
            "run_consistency_check_task",
            str(project_id),
            resolved_branch,
            job_id,
        )
        if job is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to enqueue consistency check job",
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to enqueue consistency check: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to enqueue consistency check job",
        ) from e

    return ConsistencyCheckTriggerResponse(job_id=job_id)


@router.get(
    "/{project_id}/quality/jobs/{job_id}",
    response_model=ConsistencyCheckResult,
)
async def get_quality_job_result(
    project_id: UUID,
    job_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: OptionalUser,
) -> ConsistencyCheckResult:
    """Get consistency check results by job ID."""
    await verify_project_access(project_id, db, user)

    try:
        redis = _get_redis()
        cached = await redis.get(f"quality_job:{project_id}:{job_id}")
        if cached:
            return ConsistencyCheckResult.model_validate_json(cached)
    except Exception:
        logger.warning("Failed to read quality job from Redis", exc_info=True)

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

    try:
        redis = _get_redis()
        cache_key = f"quality:{project_id}:{resolved_branch}"
        cached = await redis.get(cache_key)
        if cached:
            return ConsistencyCheckResult.model_validate_json(cached)
    except Exception:
        logger.warning("Failed to read consistency cache from Redis", exc_info=True)

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
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to enqueue duplicate detection job",
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to enqueue duplicate detection: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to enqueue duplicate detection job",
        ) from e

    return DuplicateDetectionTriggerResponse(job_id=job_id)


@router.get(
    "/{project_id}/quality/duplicates/jobs/{job_id}",
    response_model=DuplicateDetectionResult,
)
async def get_duplicate_job_result(
    project_id: UUID,
    job_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: OptionalUser,
) -> DuplicateDetectionResult:
    """Get duplicate detection results by job ID."""
    await verify_project_access(project_id, db, user)

    try:
        redis = _get_redis()
        cached = await redis.get(f"duplicates_job:{project_id}:{job_id}")
        if cached:
            return DuplicateDetectionResult.model_validate_json(cached)
    except Exception:
        logger.warning("Failed to read duplicates job from Redis", exc_info=True)

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

    try:
        redis = _get_redis()
        cached = await redis.get(f"duplicates:{project_id}:{resolved_branch}")
        if cached:
            return DuplicateDetectionResult.model_validate_json(cached)
    except Exception:
        logger.warning("Failed to read duplicates cache from Redis", exc_info=True)

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

    from ontokit.api.utils.ws_auth import authenticate_ws

    project_id_str = str(project_id)

    if not await authenticate_ws(websocket, project_id, token):
        return

    pubsub = None
    try:
        pool = await get_arq_pool()
        pubsub = pool.pubsub()
        await pubsub.subscribe(QUALITY_UPDATES_CHANNEL)

        while True:
            # Check for Redis messages (non-blocking with short timeout)
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
            await pubsub.unsubscribe(QUALITY_UPDATES_CHANNEL)
