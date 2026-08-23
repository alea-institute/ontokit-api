"""Duplicate check API — composite scoring endpoint for pre-submission validation."""

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.core.auth import RequiredUser
from ontokit.core.database import get_db
from ontokit.schemas.duplicate_check import (
    DistinctDecisionMarkRequest,
    DistinctDecisionResponse,
    DuplicateCheckRequest,
    DuplicateCheckResponse,
)
from ontokit.services.duplicate_check_service import (
    DistinctDecisionConflictError,
    DuplicateCandidateUnavailableError,
    DuplicateCheckService,
)
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
    and an enriched candidate list with branch-source provenance.

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
            proposed_iri=request.proposed_iri,
            suggestion_session_id=request.suggestion_session_id,
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


async def _require_project_role(
    project_id: UUID,
    db: AsyncSession,
    user: RequiredUser,
    allowed_roles: frozenset[str],
    detail: str,
) -> None:
    project = await get_project_service(db).get(project_id, user)
    if project.user_role not in allowed_roles and not user.is_superadmin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


@router.post(
    "/duplicate-check/distinct-decisions",
    response_model=DistinctDecisionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def mark_distinct(
    project_id: UUID,
    request: DistinctDecisionMarkRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: RequiredUser,
) -> DistinctDecisionResponse:
    """Mark a current duplicate warning as a distinct entity pair."""
    await _require_project_role(
        project_id,
        db,
        user,
        frozenset({"owner", "admin", "editor"}),
        "Only owners, admins, or editors can mark entities as distinct",
    )
    try:
        decision = await DuplicateCheckService(db).mark_distinct(
            project_id=project_id,
            request=request,
            actor_id=str(user.id),
            billing_user_id=str(user.id),
        )
    except DuplicateCandidateUnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except DistinctDecisionConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except EmbeddingBudgetExceeded as exc:
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=str(exc)) from exc
    except EmbeddingPricingUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Embedding pricing is unavailable; distinct decision cannot be validated.",
        ) from exc
    return DistinctDecisionResponse.model_validate(decision)


@router.get(
    "/duplicate-check/distinct-decisions",
    response_model=list[DistinctDecisionResponse],
)
async def list_distinct_decisions(
    project_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: RequiredUser,
    include_inactive: bool = False,
    skip: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[DistinctDecisionResponse]:
    """List active decisions, optionally including revoked audit history."""
    await _require_project_role(
        project_id,
        db,
        user,
        frozenset({"owner", "admin", "editor", "suggester", "viewer"}),
        "Project membership required to view distinct decisions",
    )
    decisions = await DuplicateCheckService(db).list_distinct_decisions(
        project_id, include_inactive=include_inactive, skip=skip, limit=limit
    )
    return [DistinctDecisionResponse.model_validate(decision) for decision in decisions]


@router.delete(
    "/duplicate-check/distinct-decisions/{decision_id}",
    response_model=DistinctDecisionResponse,
)
async def revoke_distinct_decision(
    project_id: UUID,
    decision_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: RequiredUser,
) -> DistinctDecisionResponse:
    """Revoke a decision; only owners and admins may restore detector warnings."""
    await _require_project_role(
        project_id,
        db,
        user,
        frozenset({"owner", "admin"}),
        "Only owners or admins can revoke distinct decisions",
    )
    decision = await DuplicateCheckService(db).revoke_distinct_decision(
        project_id, decision_id, str(user.id)
    )
    if decision is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Distinct decision not found",
        )
    return DistinctDecisionResponse.model_validate(decision)
