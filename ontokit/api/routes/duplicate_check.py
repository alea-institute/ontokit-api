"""Duplicate check API — composite scoring endpoint for pre-submission validation."""

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.core.auth import OptionalUser
from ontokit.core.database import get_db
from ontokit.schemas.duplicate_check import DuplicateCheckRequest, DuplicateCheckResponse
from ontokit.services.duplicate_check_service import DuplicateCheckService
from ontokit.services.project_service import get_project_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects/{project_id}", tags=["duplicate-check"])


@router.post("/duplicate-check", response_model=DuplicateCheckResponse)
async def check_duplicate(
    project_id: UUID,
    request: DuplicateCheckRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: OptionalUser,
) -> DuplicateCheckResponse:
    """Check if a proposed entity is a duplicate of anything in the ontology.

    Returns verdict (block/warn/pass), composite score, score breakdown,
    and enriched candidate list with source and rejection history.

    Used by suggestion generation (Phase 13) and inline UX (Phase 14)
    before allowing a suggestion to be submitted.

    Access mirrors the other ontology-index reads (semantic search): public
    projects are readable by anyone; private projects require membership —
    enforced by ``project_service.get`` (403/404).
    """
    # Same access rule as /search/semantic — this endpoint reads the ontology
    # index + embeddings, so it must not leak private-project entity data.
    await get_project_service(db).get(project_id, user)
    service = DuplicateCheckService(db)
    return await service.check(
        project_id=project_id,
        label=request.label,
        entity_type=request.entity_type,
        parent_iri=request.parent_iri,
        limit=10,
    )
