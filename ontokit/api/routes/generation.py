"""Generation API endpoints — LLM suggestion generation and entity validation.

Routes:
  POST /projects/{project_id}/llm/generate-suggestions
      Orchestrates full generation pipeline: context assembly → LLM → parse →
      validate → dedup → return typed suggestions.
      Enforces rate limiting, budget, BYO-key routing, and role access gate.

  POST /projects/{project_id}/llm/validate-entity
      Validates a single entity proposal against all VALID-* rules.
      Used by the frontend for user-written suggestions before submission.

Authorization pattern mirrors llm.py:
  - RequiredUser dependency for all routes
  - Project membership + LLM access gate
  - BYO key via X-BYO-API-Key header (key is never stored or logged)
"""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.core.auth import RequiredUser
from ontokit.core.database import get_db
from ontokit.models.llm_config import ProjectLLMConfig
from ontokit.models.project import Project, ProjectMember
from ontokit.schemas.generation import (
    GenerateSuggestionsRequest,
    GenerateSuggestionsResponse,
    ValidateEntityRequest,
    ValidateEntityResponse,
)
from ontokit.services.context_assembler import OntologyContextAssembler
from ontokit.services.duplicate_check_service import DuplicateCheckService
from ontokit.services.llm import (
    check_budget,
    check_llm_access,
    check_rate_limit,
    decrypt_secret,
    get_model_pricing,
    get_provider,
    log_llm_call,
)
from ontokit.services.suggestion_generation_service import SuggestionGenerationService
from ontokit.services.validation_service import ValidationService, detect_project_namespace

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects/{project_id}/llm", tags=["Generation"])


# ── Helpers ───────────────────────────────────────────────────────────────────


def _get_redis():
    """Get the shared Redis connection pool (fails open if unavailable)."""
    try:
        from ontokit.main import redis_pool  # noqa: PLC0415
        return redis_pool
    except Exception:
        return None


async def _require_project_member(
    db: AsyncSession, project_id: UUID, user_id: str, is_superadmin: bool = False
) -> str:
    """Return the user's role, raising 403 if not a member."""
    result = await db.execute(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user_id,
        )
    )
    member = result.scalar_one_or_none()
    role = member.role if member else None
    if role is None and not is_superadmin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not a member of this project",
        )
    return role or "admin"


async def _get_llm_config(db: AsyncSession, project_id: UUID) -> ProjectLLMConfig | None:
    result = await db.execute(
        select(ProjectLLMConfig).where(ProjectLLMConfig.project_id == project_id)
    )
    return result.scalar_one_or_none()


async def _load_project(db: AsyncSession, project_id: UUID) -> Project:
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )
    return project


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.post("/generate-suggestions", response_model=GenerateSuggestionsResponse)
async def generate_suggestions(
    project_id: UUID,
    request: GenerateSuggestionsRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[RequiredUser, Depends()],
    x_byo_api_key: Annotated[str | None, Header(alias="X-BYO-API-Key")] = None,
) -> GenerateSuggestionsResponse:
    """Generate LLM-powered ontology suggestions for a given class.

    Runs the full pipeline:
    1. Role gate — only owner/admin/editor/suggester may use LLM features
    2. Rate limit — per-user daily cap by role (Redis-backed, fails open)
    3. Budget — monthly + daily cap enforcement
    4. BYO-key routing — X-BYO-API-Key overrides stored project key
    5. Suggestion generation — context → LLM → parse → validate → dedup
    6. Audit log — token usage recorded without prompt/response content

    Returns typed suggestions with embedded validation status and duplicate verdicts.
    """
    # 1. Load project + role
    project = await _load_project(db, project_id)
    role = await _require_project_member(db, project_id, user.id, user.is_superadmin)

    # 2. LLM access gate (ROLE-05: anonymous / viewer blocked)
    if not check_llm_access(role, is_anonymous=getattr(user, "is_anonymous", False)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"LLM features are not available for your role ({role})",
        )

    # 3. Load LLM config — 400 if not configured
    config = await _get_llm_config(db, project_id)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No LLM configuration found for this project. Configure one in project settings.",
        )

    # 4. Rate limit check (fails open if Redis unavailable)
    redis = _get_redis()
    if redis is not None:
        within_limit = await check_rate_limit(redis, str(project_id), user.id, role)
        if not within_limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Daily LLM call limit reached for your role ({role}). Try again tomorrow.",
            )

    # 5. Budget check
    within_budget, budget_reason = await check_budget(db, str(project_id), config)
    if not within_budget:
        detail = (
            "Daily spending cap reached for this project."
            if budget_reason == "daily_cap_reached"
            else "Monthly LLM budget exhausted for this project."
        )
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=detail)

    # 6. Resolve API key: BYO wins (never stored), else decrypt project key
    if x_byo_api_key:
        api_key = x_byo_api_key  # ephemeral — never logged, never stored
    elif config.api_key_encrypted:
        api_key = decrypt_secret(config.api_key_encrypted)
    else:
        api_key = None

    # 7. Get provider
    try:
        provider = get_provider(
            provider_type=config.provider,
            api_key=api_key,
            base_url=config.base_url,
            model=config.model,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid LLM provider configuration: {exc}",
        ) from exc

    # 8. Detect project namespace for IRI minting (VALID-06 / D-12)
    project_namespace = await detect_project_namespace(
        project.ontology_iri, db, project_id, request.branch
    )

    # 9. Construct services
    assembler = OntologyContextAssembler(db)
    validator = ValidationService(db)
    dedup = DuplicateCheckService(db)
    svc = SuggestionGenerationService(
        db=db,
        assembler=assembler,
        validator=validator,
        dedup_service=dedup,
    )

    # 10. Run generation pipeline
    try:
        response = await svc.generate(
            project_id=project_id,
            branch=request.branch,
            class_iri=request.class_iri,
            suggestion_type=request.suggestion_type,
            batch_size=request.batch_size,
            provider=provider,
            project_namespace=project_namespace,
        )
    except ValueError as exc:
        # Raised by OntologyContextAssembler when class_iri not found in index
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        # LLM provider auth errors → 502
        error_msg = str(exc)
        if api_key and api_key in error_msg:
            error_msg = error_msg.replace(api_key, "[REDACTED]")
        # Check for auth-like errors
        if any(
            keyword in error_msg.lower()
            for keyword in ("unauthorized", "authentication", "api key", "401", "403", "forbidden")
        ):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"LLM provider authentication error: {error_msg}",
            ) from exc
        # JSON decode / timeout → return empty suggestions (not 500)
        logger.warning(
            "generate_suggestions: non-fatal error for project %s: %s",
            project_id, error_msg
        )
        return GenerateSuggestionsResponse(
            suggestions=[],
            input_tokens=0,
            output_tokens=0,
            context_tokens_estimate=None,
        )

    # 11. Audit log — metadata only, never prompt/response content (D-08)
    try:
        model_id = config.model or ""
        input_cost_per_tok, output_cost_per_tok = await get_model_pricing(model_id)
        cost_estimate = (
            response.input_tokens * input_cost_per_tok
            + response.output_tokens * output_cost_per_tok
        )
        await log_llm_call(
            db=db,
            project_id=str(project_id),
            user_id=user.id,
            model=model_id,
            provider=str(config.provider),
            endpoint="llm/generate-suggestions",
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cost_estimate_usd=cost_estimate,
            is_byo_key=bool(x_byo_api_key),
        )
        await db.commit()
    except Exception as exc:
        logger.warning("generate_suggestions: audit log failed: %s", exc)
        # Don't fail the request if audit logging fails

    return response


@router.post("/validate-entity", response_model=ValidateEntityResponse)
async def validate_entity(
    project_id: UUID,
    request: ValidateEntityRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[RequiredUser, Depends()],
) -> ValidateEntityResponse:
    """Validate a single entity proposal against all VALID-* rules.

    Used by the frontend for user-written suggestions before they enter the
    draft/session flow (D-08). Generated suggestions are auto-validated inside
    the generation pipeline (D-09) so this endpoint is primarily for
    manually-authored proposals.

    Does NOT require LLM configuration — validation is pure server-side logic.
    """
    # Check project membership (any member can validate)
    project = await _load_project(db, project_id)
    await _require_project_member(db, project_id, user.id, user.is_superadmin)

    # Detect or derive project namespace
    branch = "main"  # validate-entity doesn't require a branch param; default to main
    project_namespace = request.namespace or await detect_project_namespace(
        project.ontology_iri, db, project_id, branch
    )

    # Build entity dict matching ValidationService.validate_entity() expectations
    entity: dict = {
        "iri": request.entity_iri or "",
        "label": request.label,
        "parent_iris": request.parent_iris,
        "labels": request.labels,
    }

    # Run all VALID-* rules
    validator = ValidationService(db)
    errors = await validator.validate_entity(
        project_id=project_id,
        branch=branch,
        entity=entity,
        project_namespace=project_namespace,
    )

    return ValidateEntityResponse(valid=len(errors) == 0, errors=errors)
