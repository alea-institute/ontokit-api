"""Semantic search and similarity API endpoints."""

import logging
from typing import Annotated
from urllib.parse import unquote
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.core.auth import CurrentUser, RequiredUser
from ontokit.core.database import get_db
from ontokit.schemas.embeddings import (
    RankedCandidate,
    RankSuggestionRequest,
    SemanticSearchResponse,
    SimilarEntity,
)
from ontokit.services.embedding_service import (
    EmbeddingBudgetExceeded,
    EmbeddingPricingUnavailable,
    EmbeddingService,
)
from ontokit.services.project_service import get_project_service

logger = logging.getLogger(__name__)

router = APIRouter()


def get_embeddings(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> EmbeddingService:
    """Dependency to get embedding service with database session."""
    return EmbeddingService(db)


async def _verify_access(project_id: UUID, db: AsyncSession, user: CurrentUser) -> None:
    service = get_project_service(db)
    project = await service.get(project_id, user)
    if project.user_role is None and not user.is_superadmin:
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Project membership required for embedding-powered search",
        )


@router.get(
    "/{project_id}/search/semantic",
    response_model=SemanticSearchResponse,
)
async def semantic_search(
    project_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[EmbeddingService, Depends(get_embeddings)],
    user: RequiredUser,
    q: str = Query(..., min_length=1, description="Search query"),
    branch: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    threshold: float = Query(default=0.3, ge=0.0, le=1.0),
) -> SemanticSearchResponse:
    """Search entities using semantic similarity."""
    await _verify_access(project_id, db, user)
    resolved_branch = branch
    if not resolved_branch:
        from ontokit.git import get_git_service

        resolved_branch = get_git_service().get_default_branch(project_id)
    try:
        return await service.semantic_search(
            project_id,
            resolved_branch,
            q,
            limit,
            threshold,
            billing_user_id=str(user.id),
        )
    except EmbeddingBudgetExceeded as exc:
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=str(exc)) from exc
    except EmbeddingPricingUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Embedding pricing is unavailable; search is paused.",
        ) from exc


@router.get(
    "/{project_id}/entities/{iri:path}/similar",
    response_model=list[SimilarEntity],
)
async def find_similar_entities(
    project_id: UUID,
    iri: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[EmbeddingService, Depends(get_embeddings)],
    user: RequiredUser,
    branch: str | None = Query(default=None),
    limit: int = Query(default=10, ge=1, le=50),
    threshold: float = Query(default=0.5, ge=0.0, le=1.0),
) -> list[SimilarEntity]:
    """Find entities similar to a given entity."""
    await _verify_access(project_id, db, user)
    resolved_branch = branch
    if not resolved_branch:
        from ontokit.git import get_git_service

        resolved_branch = get_git_service().get_default_branch(project_id)
    decoded_iri = unquote(iri)
    return await service.find_similar(project_id, resolved_branch, decoded_iri, limit, threshold)


@router.post(
    "/{project_id}/entities/rank-suggestions",
    response_model=list[RankedCandidate],
)
async def rank_suggestions(
    project_id: UUID,
    body: RankSuggestionRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[EmbeddingService, Depends(get_embeddings)],
    user: RequiredUser,
) -> list[RankedCandidate]:
    """Rank candidate entities by similarity to a context entity."""
    await _verify_access(project_id, db, user)
    if not body.branch:
        from ontokit.git import get_git_service

        body.branch = get_git_service().get_default_branch(project_id)
    return await service.rank_suggestions(project_id, body)
