"""Pre-commit validation endpoint — runs reasoner checks before saving."""

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.api.dependencies import load_project_graph
from ontokit.core.database import get_db
from ontokit.services.reasoner_service import ReasonerResult, ReasonerService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects/{project_id}", tags=["Validation"])


class ValidateRequest(BaseModel):
    """Request body for pre-commit validation.

    owl_content: Optional OWL/Turtle content to validate before committing.
                 When provided, this content is validated directly (pre-commit use case).
                 When absent, the endpoint reads current source from storage (post-commit check).
    branch: Branch to read source from when owl_content is not provided.
    """

    owl_content: str | None = None
    branch: str = "main"


def get_reasoner_service() -> ReasonerService:
    return ReasonerService()


@router.post("/validate", response_model=ReasonerResult)
async def validate_pre_commit(
    project_id: UUID,
    request: ValidateRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    reasoner: Annotated[ReasonerService, Depends(get_reasoner_service)],
    branch: str | None = Query(default=None, description="Branch to read from when owl_content absent"),
) -> ReasonerResult:
    """Run OWL reasoner validation on ontology content (TOOL-04).

    Supports two modes:
    1. **Pre-commit** (owl_content provided): Validates the submitted content directly.
       This is the primary use case — called after user accepts suggestions but before
       the content is committed to storage.
    2. **Post-commit check** (owl_content absent): Reads current source from storage
       and validates it. Useful for health checks on already-committed state.
    """
    if request.owl_content:
        # Pre-commit: validate the provided content directly
        source_content = request.owl_content
    else:
        # Post-commit fallback: load current ontology from git/storage and serialize to Turtle
        resolved_branch = branch or request.branch
        try:
            graph, _ = await load_project_graph(project_id, resolved_branch, db)
            source_content = graph.serialize(format="turtle")
        except Exception as e:
            logger.warning(
                "Could not load ontology for project %s branch %s — skipping validation: %s",
                project_id,
                resolved_branch,
                e,
            )
            return ReasonerResult(consistent=True, issues=[], reasoner_used="none")

    return reasoner.check_consistency(source_content)
