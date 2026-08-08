"""Duplicate check API — composite scoring endpoint for pre-submission validation."""

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.core.auth import RequiredUser
from ontokit.core.database import get_db
from ontokit.schemas.duplicate_check import DuplicateCheckRequest, DuplicateCheckResponse
from ontokit.services.duplicate_check_service import DuplicateCheckService
from ontokit.services.embedding_service import (
    EmbeddingBudgetExceeded,
    EmbeddingPricingUnavailable,
)
from ontokit.services.project_service import get_project_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects/{project_id}", tags=["duplicate-check"])


@router.post("/duplicate-check", response_model=DuplicateCheckResponse)
async def check_duplicate(
    project_id: UUID,
    request: DuplicateCheckRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: RequiredUser,
) -> DuplicateCheckResponse:
    """Check if a proposed entity is a duplicate of anything in the ontology.

    Returns verdict (block/warn/pass), composite score, score breakdown,
    and enriched candidate list with source and rejection history.

    Used by suggestion generation (Phase 13) and inline UX (Phase 14)
    before allowing a suggestion to be submitted.

    Embedding-backed checks may spend a stored project key, so authentication
    and project membership are required even when the project is public.
    """
    # Same access rule as /search/semantic — this endpoint reads the ontology
    # index + embeddings, so it must not leak private-project entity data.
    project = await get_project_service(db).get(project_id, user)
    if project.user_role is None and not user.is_superadmin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Project membership required for duplicate checks",
        )
    service = DuplicateCheckService(db)
    try:
        return await service.check(
            project_id=project_id,
            label=request.label,
            entity_type=request.entity_type,
            parent_iri=request.parent_iri,
            limit=10,
            billing_user_id=str(user.id),
        )
    except EmbeddingBudgetExceeded as exc:
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=str(exc)) from exc
    except EmbeddingPricingUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Embedding pricing is unavailable; duplicate check is paused.",
        ) from exc
