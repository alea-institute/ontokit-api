"""Embedding management API endpoints."""

import logging
import uuid
from typing import Annotated
from uuid import UUID

from arq import ArqRedis, create_pool
from arq.connections import RedisSettings
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.core.auth import RequiredUser
from ontokit.core.config import settings
from ontokit.core.database import get_db
from ontokit.schemas.embeddings import (
    EmbeddingConfig,
    EmbeddingConfigUpdate,
    EmbeddingGenerateResponse,
    EmbeddingStatus,
)
from ontokit.services.embedding_service import EmbeddingService
from ontokit.services.project_service import get_project_service

logger = logging.getLogger(__name__)

router = APIRouter()

_arq_pool: ArqRedis | None = None


async def get_arq_pool() -> ArqRedis:
    global _arq_pool
    if _arq_pool is None:
        from urllib.parse import urlparse

        redis_url = str(settings.redis_url)
        parsed = urlparse(redis_url)
        redis_settings = RedisSettings(
            host=parsed.hostname or "localhost",
            port=parsed.port or 6379,
            database=int(parsed.path.lstrip("/") or "0"),
        )
        _arq_pool = await create_pool(redis_settings)
    return _arq_pool


async def _verify_write_access(project_id: UUID, db: AsyncSession, user):
    service = get_project_service(db)
    project_response = await service.get(project_id, user)
    if project_response.user_role not in ("owner", "admin", "editor"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Write access required",
        )


@router.get("/{project_id}/embeddings/config", response_model=EmbeddingConfig)
async def get_embedding_config(
    project_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: RequiredUser,
) -> EmbeddingConfig:
    """Get embedding configuration for a project."""
    service = get_project_service(db)
    await service.get(project_id, user)

    embed_service = EmbeddingService(db)
    config = await embed_service.get_config(project_id)
    if not config:
        return EmbeddingConfig(
            provider="local",
            model_name="all-MiniLM-L6-v2",
            api_key_set=False,
            dimensions=384,
            auto_embed_on_save=False,
        )
    return config


@router.put("/{project_id}/embeddings/config", response_model=EmbeddingConfig)
async def update_embedding_config(
    project_id: UUID,
    data: EmbeddingConfigUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: RequiredUser,
) -> EmbeddingConfig:
    """Update embedding configuration."""
    await _verify_write_access(project_id, db, user)
    embed_service = EmbeddingService(db)
    return await embed_service.update_config(project_id, data)


@router.post(
    "/{project_id}/embeddings/generate",
    response_model=EmbeddingGenerateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def generate_embeddings(
    project_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: RequiredUser,
    branch: str = Query(default="main"),
) -> EmbeddingGenerateResponse:
    """Trigger full project embedding generation (background job)."""
    await _verify_write_access(project_id, db, user)

    job_id = uuid.uuid4()
    pool = await get_arq_pool()
    await pool.enqueue_job(
        "run_embedding_generation_task",
        str(project_id),
        branch,
        str(job_id),
    )
    return EmbeddingGenerateResponse(job_id=str(job_id))


@router.get("/{project_id}/embeddings/status", response_model=EmbeddingStatus)
async def get_embedding_status(
    project_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: RequiredUser,
    branch: str = Query(default="main"),
) -> EmbeddingStatus:
    """Get embedding status and coverage."""
    service = get_project_service(db)
    await service.get(project_id, user)

    embed_service = EmbeddingService(db)
    return await embed_service.get_status(project_id, branch)


@router.delete(
    "/{project_id}/embeddings",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def clear_embeddings(
    project_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: RequiredUser,
) -> None:
    """Clear all embeddings for a project."""
    await _verify_write_access(project_id, db, user)
    embed_service = EmbeddingService(db)
    await embed_service.clear_embeddings(project_id)
