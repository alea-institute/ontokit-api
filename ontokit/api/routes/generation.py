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
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.api.routes.llm import _LOCAL_PROVIDERS
from ontokit.core.auth import RequiredUser
from ontokit.core.database import get_db
from ontokit.models.llm_config import ProjectLLMConfig
from ontokit.models.project import Project, ProjectMember
from ontokit.models.suggestion_session import SuggestionSession, SuggestionSessionStatus
from ontokit.schemas.generation import (
    GenerateSuggestionsRequest,
    GenerateSuggestionsResponse,
    ValidateEntityRequest,
    ValidateEntityResponse,
)
from ontokit.services.context_assembler import OntologyContextAssembler
from ontokit.services.duplicate_check_service import DuplicateCheckService
from ontokit.services.llm import (
    BudgetLimits,
    LLMBudgetExceeded,
    MeteredLLMProvider,
    PricingUnavailableError,
    check_budget,
    check_llm_access,
    check_rate_limit,
    decrypt_secret,
    get_model_pricing,
    get_provider,
)
from ontokit.services.llm.rate_limiter import FAIL_OPEN_EVENT
from ontokit.services.project_access_policy import (
    load_visible_project,
    require_visible_project,
    visible_project_clause,
)
from ontokit.services.suggestion_generation_service import SuggestionGenerationService
from ontokit.services.validation_service import ValidationService, detect_project_namespace

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects/{project_id}/llm", tags=["Generation"])


# ── Helpers ───────────────────────────────────────────────────────────────────


def _get_redis() -> Any:
    """Get the shared Redis connection pool, or None if unavailable.

    Returns None when the pool cannot be imported (app not fully initialised).
    The caller treats None as fail-open for rate limiting, but must log the
    bypass at WARNING so ops can alert on unmetered LLM traffic (the DB budget
    layer is the non-fail-open backstop). Narrowed to Import/attribute errors:
    a mis-wired pool object raising elsewhere should surface, not be swallowed.
    """
    try:
        from ontokit.main import redis_pool

        return redis_pool
    except (ImportError, AttributeError) as exc:
        logger.warning("Rate-limit Redis pool unavailable (%s) — rate limiting will fail open", exc)
        return None


async def _require_project_member(
    db: AsyncSession, project_id: UUID, user_id: str, is_superadmin: bool = False
) -> str:
    """Return the user's role, raising 403 if not a member."""
    result = await db.execute(
        select(ProjectMember)
        .join(Project, Project.id == ProjectMember.project_id)
        .where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user_id,
            visible_project_clause(),
        )
    )
    member = result.scalar_one_or_none()
    role = member.role if member else None
    if role is None:
        if is_superadmin:
            await load_visible_project(db, project_id)
        else:
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
    require_visible_project(project)
    return project


async def _mark_active_session_llm_generated(
    db: AsyncSession, project_id: UUID, user_id: str, branch: str
) -> bool:
    """Persist sticky LLM provenance on the matching active suggestion session.

    Generation can also run against an ordinary project branch. In that case
    the constrained UPDATE matches no row and remains harmless. Once set, the
    flag is never cleared, so later submission cannot schedule that session for
    quiet-period auto-accept.
    """
    result = await db.execute(
        update(SuggestionSession)
        .where(
            SuggestionSession.project_id == project_id,
            SuggestionSession.user_id == user_id,
            SuggestionSession.branch == branch,
            SuggestionSession.status == SuggestionSessionStatus.ACTIVE.value,
        )
        .values(is_llm_generated=True)
    )
    if result.rowcount == 0:  # type: ignore[attr-defined]
        return False
    await db.commit()
    return True


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.post("/generate-suggestions", response_model=GenerateSuggestionsResponse)
async def generate_suggestions(
    project_id: UUID,
    request: GenerateSuggestionsRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: RequiredUser,
    x_byo_api_key: Annotated[str | None, Header(alias="X-BYO-API-Key")] = None,
) -> GenerateSuggestionsResponse:
    """Generate LLM-powered ontology suggestions for a given class.

    Runs the full pipeline:
    1. Role gate — only owner/admin/editor/suggester may use LLM features
    2. Rate limit — per-user daily cap by role (Redis-backed, fails open)
    3. Budget — monthly + daily cap enforcement
    4. BYO-key routing — X-BYO-API-Key overrides stored project key
    5. Suggestion generation — context → LLM → parse → validate → dedup
    6. Audit log — spend reserved atomically before provider actuation and
       reconciled to actual token usage without prompt/response content

    Returns typed suggestions with embedded validation status, duplicate verdicts,
    and per-suggestion model + prompt-template provenance (metadata only — the raw
    prompt text is never persisted, per D-08).
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
    # Provenance completeness (D-08): every suggestion must carry a resolvable
    # model id. `config.model` is nullable, so refuse to generate without one
    # rather than stamp `model=None` on the output.
    if not config.model:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No model selected for this project's LLM configuration. Choose one in project settings.",
        )
    budget_limits = BudgetLimits(
        monthly_budget_usd=config.monthly_budget_usd,  # pyright: ignore[reportArgumentType]
        daily_cap_usd=config.daily_cap_usd,  # pyright: ignore[reportArgumentType]
    )

    # Resolve trustworthy pricing before any provider call. Unknown models and
    # pricing outages fail closed so the dollar budget cannot silently become
    # an unlimited $0 ledger.
    if config.provider in _LOCAL_PROVIDERS:
        input_cost_per_tok, output_cost_per_tok = (0.0, 0.0)
    else:
        try:
            input_cost_per_tok, output_cost_per_tok = await get_model_pricing(config.model)
        except PricingUnavailableError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Pricing data is unavailable for the selected model; generation is paused.",
            ) from exc

    # 4. Rate limit check (fails open if Redis unavailable — DB budget in step 5
    #    is the non-fail-open backstop). Both fail-open paths (pool absent here,
    #    and Redis infra errors inside check_rate_limit) log at WARNING so ops
    #    can alert on unmetered LLM traffic — see PR-4 review follow-up.
    redis = _get_redis()
    if redis is not None:
        within_limit = await check_rate_limit(redis, str(project_id), user.id, role)
        if not within_limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Daily LLM call limit reached for your role ({role}). Try again tomorrow.",
            )
    else:
        # Pool absent → the route-level fail-open. Emit the SAME actionable marker
        # the limiter-internal fail-open paths use, so ops alert on one signal.
        logger.warning(
            "ALERT %s: rate limiter failed open during route_rate_limit_bypass "
            "(Redis pool absent, call allowed; budget cap still enforced) "
            "— project=%s user=%s",
            FAIL_OPEN_EVENT,
            project_id,
            user.id,
            extra={
                "event": FAIL_OPEN_EVENT,
                "operation": "route_rate_limit_bypass",
                "project_id": str(project_id),
                "user_id": user.id,
            },
        )

    # 5. Budget check
    within_budget, budget_reason = await check_budget(db, project_id, budget_limits)
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
    provider = MeteredLLMProvider(
        provider,
        project_id=project_id,
        config=budget_limits,
        user_id=user.id,
        model=config.model,
        provider_name=str(config.provider),
        endpoint="llm/generate-suggestions",
        input_cost_per_token=input_cost_per_tok,
        output_cost_per_token=output_cost_per_tok,
        is_byo_key=bool(x_byo_api_key),
    )

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
            model_id=config.model,
        )
    except LLMBudgetExceeded as exc:
        detail = (
            "Daily spending cap reached for this project."
            if exc.reason == "daily_cap_reached"
            else "Monthly LLM budget exhausted for this project."
        )
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=detail) from exc
    except ValueError as exc:
        # Raised by OntologyContextAssembler when class_iri not found in index
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        # Redact the resolved key from any error text before it is logged or
        # (for transient cases) surfaced.
        error_msg = str(exc)
        if api_key and api_key in error_msg:
            error_msg = error_msg.replace(api_key, "[REDACTED]")

        # Provider auth failures → 502. Return a GENERIC client message; the
        # (redacted) provider detail is logged server-side only, never echoed to
        # the caller (avoids leaking provider internals / partial secrets).
        if any(
            keyword in error_msg.lower()
            for keyword in ("unauthorized", "authentication", "api key", "401", "403", "forbidden")
        ):
            logger.warning(
                "generate_suggestions: provider auth error for project %s: %s",
                project_id,
                error_msg,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="LLM provider rejected the request (authentication failed). "
                "Verify the project's API key.",
            ) from exc

        # Transient provider/network hiccups → empty suggestions (not a 500), so a
        # flaky upstream doesn't hard-fail the editor.
        if isinstance(exc, TimeoutError | ConnectionError):
            logger.warning(
                "generate_suggestions: transient provider error for project %s: %s",
                project_id,
                error_msg,
            )
            return GenerateSuggestionsResponse(
                suggestions=[],
                input_tokens=0,
                output_tokens=0,
                context_tokens_estimate=None,
            )

        # Anything else is an unexpected bug — do NOT mask it as an empty 200.
        # Let it surface as a 500 so regressions are visible (review MEDIUM #5).
        logger.error(
            "generate_suggestions: unexpected error for project %s: %s",
            project_id,
            error_msg,
            exc_info=True,
        )
        raise

    # A successful generation that produced proposals permanently marks the
    # matching active suggestion session. This server-owned provenance is what
    # the trust scheduler reads; client flags cannot opt LLM work back in.
    if response.suggestions:
        await _mark_active_session_llm_generated(db, project_id, user.id, request.branch)

    return response


@router.post("/validate-entity", response_model=ValidateEntityResponse)
async def validate_entity(
    project_id: UUID,
    request: ValidateEntityRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: RequiredUser,
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
    entity: dict[str, Any] = {
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
