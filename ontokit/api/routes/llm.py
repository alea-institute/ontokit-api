"""FastAPI routes for LLM configuration, status, usage, and provider metadata.

Routes:
  GET  /projects/{project_id}/llm/config       — project LLM config (any member)
  PUT  /projects/{project_id}/llm/config       — update LLM config (owner/admin only)
  POST /projects/{project_id}/llm/test-connection — test provider connectivity (owner/admin)
  GET  /projects/{project_id}/llm/status       — LLM feature status for current user
  GET  /projects/{project_id}/llm/usage        — usage dashboard (owner/admin only)
  PATCH /projects/{project_id}/members/{user_id}/flags — toggle can_self_merge_structural
  GET  /llm/providers                          — static provider list (public)
  GET  /llm/known-models                       — static known-models list (public)

Authorization pattern mirrors embeddings.py:
- RequiredUser dependency for authenticated routes
- Role check via ProjectService._get_user_role or direct DB query
- Owner/admin check: role not in ("owner", "admin") → 403
- LLM access check: check_llm_access(role) → 403
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.core.auth import RequiredUser
from ontokit.core.database import get_db
from ontokit.models.llm_config import LLMAuditLog, ProjectLLMConfig
from ontokit.models.project import Project, ProjectMember
from ontokit.schemas.llm import (
    LLMConfigResponse,
    LLMConfigUpdate,
    LLMKnownModel,
    LLMProviderInfo,
    LLMProviderType,
    LLMStatusResponse,
    LLMUsageResponse,
)
from ontokit.services.llm import (
    check_budget,
    check_llm_access,
    check_rate_limit,
    decrypt_secret,
    encrypt_secret,
    get_budget_status,
    get_provider,
    get_remaining_calls,
    get_usage_summary,
    validate_base_url,
)
from ontokit.services.llm.registry import (
    KNOWN_MODELS,
    PROVIDER_DISPLAY_NAMES,
    PROVIDER_ICON_NAMES,
    PROVIDER_REQUIRES_KEY,
)

logger = logging.getLogger(__name__)

# Project-scoped routes (registered with prefix="/projects")
router = APIRouter()

# Public catalogue routes (registered at root — no auth required)
public_router = APIRouter()

# Providers that are considered local (allow private IPs, allow HTTP)
_LOCAL_PROVIDERS = {
    LLMProviderType.ollama,
    LLMProviderType.lmstudio,
    LLMProviderType.llamafile,
    LLMProviderType.custom,
}


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _get_member_role(
    db: AsyncSession, project_id: UUID, user_id: str
) -> str | None:
    """Return the user's role in the project, or None if not a member."""
    result = await db.execute(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user_id,
        )
    )
    member = result.scalar_one_or_none()
    return member.role if member else None


async def _require_project_member(
    db: AsyncSession, project_id: UUID, user_id: str, is_superadmin: bool = False
) -> str:
    """Return the user's role, raising 403 if not a member."""
    role = await _get_member_role(db, project_id, user_id)
    if role is None and not is_superadmin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not a member of this project",
        )
    return role or "admin"  # superadmins get admin-level access


async def _require_owner_or_admin(
    db: AsyncSession, project_id: UUID, user_id: str, is_superadmin: bool = False
) -> str:
    """Return the user's role, raising 403 if not owner/admin."""
    role = await _require_project_member(db, project_id, user_id, is_superadmin)
    if role not in ("owner", "admin") and not is_superadmin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only owner or admin can perform this action",
        )
    return role


async def _get_llm_config(
    db: AsyncSession, project_id: UUID
) -> ProjectLLMConfig | None:
    """Fetch the project's LLM config row, or None if not configured."""
    result = await db.execute(
        select(ProjectLLMConfig).where(ProjectLLMConfig.project_id == project_id)
    )
    return result.scalar_one_or_none()


def _config_to_response(config: ProjectLLMConfig) -> LLMConfigResponse:
    """Convert a ProjectLLMConfig DB row to the public response schema."""
    return LLMConfigResponse(
        provider=LLMProviderType(config.provider),
        model=config.model,
        model_tier=config.model_tier,
        api_key_set=bool(config.api_key_encrypted),  # NEVER return the key itself
        base_url=config.base_url,
        monthly_budget_usd=config.monthly_budget_usd,
        daily_cap_usd=config.daily_cap_usd,
    )


# ── Project-scoped LLM routes ─────────────────────────────────────────────────


@router.get("/{project_id}/llm/config", response_model=LLMConfigResponse)
async def get_llm_config(
    project_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: RequiredUser,
) -> LLMConfigResponse:
    """Get the LLM configuration for a project.

    Accessible to any project member. The API key is NEVER returned — only
    api_key_set=True/False indicates whether a key is stored.
    """
    await _require_project_member(db, project_id, user.id, user.is_superadmin)

    config = await _get_llm_config(db, project_id)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No LLM configuration found for this project",
        )

    return _config_to_response(config)


@router.put("/{project_id}/llm/config", response_model=LLMConfigResponse)
async def update_llm_config(
    project_id: UUID,
    data: LLMConfigUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: RequiredUser,
) -> LLMConfigResponse:
    """Create or update the LLM configuration for a project.

    Owner/admin only. If an API key is provided it is encrypted before storage
    and never returned in any response. The existing key is preserved if no new
    key is provided.
    """
    await _require_owner_or_admin(db, project_id, user.id, user.is_superadmin)

    # Validate base_url if provided
    if data.base_url:
        provider = data.provider
        allow_private = provider in _LOCAL_PROVIDERS if provider else False
        try:
            validate_base_url(data.base_url, allow_private=allow_private)
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid base_url: {e}",
            ) from e

    config = await _get_llm_config(db, project_id)

    if config is None:
        # Create new config
        config = ProjectLLMConfig(
            id=uuid.uuid4(),
            project_id=project_id,
            provider=(data.provider or LLMProviderType.openai).value,
            model=data.model,
            model_tier=data.model_tier or "quality",
            base_url=data.base_url,
            monthly_budget_usd=data.monthly_budget_usd,
            daily_cap_usd=data.daily_cap_usd,
        )
        if data.api_key:
            config.api_key_encrypted = encrypt_secret(data.api_key)
        db.add(config)
    else:
        # Update existing config — only apply fields that were explicitly set
        if data.provider is not None:
            config.provider = data.provider.value
        if data.model is not None:
            config.model = data.model
        if data.model_tier is not None:
            config.model_tier = data.model_tier
        if data.base_url is not None:
            config.base_url = data.base_url
        if data.monthly_budget_usd is not None:
            config.monthly_budget_usd = data.monthly_budget_usd
        if data.daily_cap_usd is not None:
            config.daily_cap_usd = data.daily_cap_usd
        if data.api_key:
            # Encrypt and overwrite; NEVER store plaintext
            config.api_key_encrypted = encrypt_secret(data.api_key)

    await db.commit()
    await db.refresh(config)
    return _config_to_response(config)


@router.post("/{project_id}/llm/test-connection")
async def test_llm_connection(
    project_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: RequiredUser,
    x_byo_api_key: Annotated[str | None, Header(alias="X-BYO-API-Key")] = None,
) -> dict:
    """Test connectivity to the configured LLM provider.

    Owner/admin only. If an X-BYO-API-Key header is present it is used for the
    test instead of the stored project key. The BYO key is NEVER stored or logged.

    Returns:
        {"success": true} on success.
        {"success": false, "error": "<message>"} on failure.
    """
    await _require_owner_or_admin(db, project_id, user.id, user.is_superadmin)

    config = await _get_llm_config(db, project_id)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No LLM configuration found for this project",
        )

    # Resolve API key: BYO header wins over stored key (BYO key is NEVER stored)
    if x_byo_api_key:
        api_key = x_byo_api_key  # ephemeral — never logged, never stored
    elif config.api_key_encrypted:
        api_key = decrypt_secret(config.api_key_encrypted)
    else:
        api_key = None

    try:
        provider = get_provider(
            provider_type=config.provider,
            api_key=api_key,
            base_url=config.base_url,
            model=config.model,
        )
        # 10-second timeout per spec
        await asyncio.wait_for(provider.test_connection(), timeout=10.0)
        return {"success": True}
    except asyncio.TimeoutError:
        return {"success": False, "error": "Connection timed out (10s limit)"}
    except Exception as exc:
        # Return the error message without leaking the key
        error_msg = str(exc)
        # Sanitize: remove any key-looking tokens from error output (Pitfall 4)
        if api_key and api_key in error_msg:
            error_msg = error_msg.replace(api_key, "[REDACTED]")
        return {"success": False, "error": error_msg}


@router.get("/{project_id}/llm/status", response_model=LLMStatusResponse)
async def get_llm_status(
    project_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: RequiredUser,
) -> LLMStatusResponse:
    """Return LLM feature availability status for the current user in this project.

    Accessible to any authenticated project member. Reports budget exhaustion
    and per-user daily remaining calls based on the caller's role.
    """
    role = await _require_project_member(db, project_id, user.id, user.is_superadmin)

    config = await _get_llm_config(db, project_id)

    # configured = has a config row with either an API key or a local provider
    configured = False
    provider_type = None
    if config:
        provider_enum = LLMProviderType(config.provider)
        provider_type = provider_enum
        is_local = provider_enum in _LOCAL_PROVIDERS
        configured = bool(config.api_key_encrypted) or is_local

    # Budget and spend
    budget_exhausted = False
    monthly_spent_usd = 0.0
    monthly_budget_usd = None
    burn_rate_daily = 0.0

    if config:
        budget_status = await get_budget_status(db, str(project_id), config)
        budget_exhausted = budget_status["budget_exhausted"]
        monthly_spent_usd = budget_status["monthly_spent_usd"]
        monthly_budget_usd = budget_status["monthly_budget_usd"]
        burn_rate_daily = budget_status["burn_rate_daily_usd"]

    # Daily remaining calls for this user's role
    daily_remaining: int | None = None
    if configured and check_llm_access(role):
        # We cannot call Redis here without an async Redis client dependency.
        # The status route reports the *limit* as remaining when we can't reach Redis.
        # Callers should check the rate limiter directly for real-time remaining counts.
        from ontokit.services.llm.rate_limiter import RATE_LIMITS
        limit = RATE_LIMITS.get(role)
        # None = unlimited, 0 = no access, int = capped
        if limit == 0:
            daily_remaining = 0
        elif limit is None:
            daily_remaining = None  # unlimited
        else:
            daily_remaining = limit  # fallback without Redis context

    return LLMStatusResponse(
        configured=configured,
        provider=provider_type,
        budget_exhausted=budget_exhausted,
        daily_remaining=daily_remaining,
        monthly_budget_usd=monthly_budget_usd,
        monthly_spent_usd=monthly_spent_usd,
        burn_rate_daily_usd=burn_rate_daily,
    )


@router.get("/{project_id}/llm/usage", response_model=LLMUsageResponse)
async def get_llm_usage(
    project_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: RequiredUser,
) -> LLMUsageResponse:
    """Return the LLM usage dashboard for a project (owner/admin only).

    Shows per-user call counts, costs, and overall budget consumption for
    the current calendar month.
    """
    await _require_owner_or_admin(db, project_id, user.id, user.is_superadmin)

    config = await _get_llm_config(db, project_id)
    usage = await get_usage_summary(db, str(project_id))

    # Patch in budget_consumed_pct using the config context
    if config and config.monthly_budget_usd and config.monthly_budget_usd > 0:
        usage.budget_consumed_pct = round(
            (usage.total_cost_usd / config.monthly_budget_usd) * 100, 2
        )

    return usage


# ── Member flag route ─────────────────────────────────────────────────────────


class _MemberFlagsUpdate:
    """Thin struct for the member flags PATCH body."""

    def __init__(self, can_self_merge_structural: bool) -> None:
        self.can_self_merge_structural = can_self_merge_structural


from pydantic import BaseModel as _BaseModel


class MemberFlagsUpdate(_BaseModel):
    """Request body for updating a member's capability flags."""

    can_self_merge_structural: bool


class MemberFlagsResponse(_BaseModel):
    """Response after updating member flags."""

    user_id: str
    can_self_merge_structural: bool


@router.patch(
    "/{project_id}/members/{target_user_id}/flags",
    response_model=MemberFlagsResponse,
)
async def update_member_flags(
    project_id: UUID,
    target_user_id: str,
    data: MemberFlagsUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: RequiredUser,
) -> MemberFlagsResponse:
    """Toggle per-member capability flags (owner/admin only).

    Currently supports:
    - can_self_merge_structural: Allow an editor to self-merge structural PRs
      without peer review (ROLE-03).
    """
    await _require_owner_or_admin(db, project_id, user.id, user.is_superadmin)

    result = await db.execute(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == target_user_id,
        )
    )
    member = result.scalar_one_or_none()
    if member is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Member not found in this project",
        )

    member.can_self_merge_structural = data.can_self_merge_structural
    await db.commit()
    await db.refresh(member)

    return MemberFlagsResponse(
        user_id=member.user_id,
        can_self_merge_structural=member.can_self_merge_structural,
    )


# ── Public (no-auth) provider/model catalogue routes ─────────────────────────


@public_router.get("/llm/providers", response_model=list[LLMProviderInfo])
async def list_llm_providers() -> list[LLMProviderInfo]:
    """Return static metadata for all supported LLM providers.

    This endpoint requires no authentication — used to populate the
    provider picker in the onboarding flow and settings UI.
    """
    return [
        LLMProviderInfo(
            provider=provider,
            display_name=PROVIDER_DISPLAY_NAMES[provider],
            requires_api_key=PROVIDER_REQUIRES_KEY[provider],
            icon_name=PROVIDER_ICON_NAMES[provider],
        )
        for provider in LLMProviderType
    ]


@public_router.get("/llm/known-models", response_model=list[LLMKnownModel])
async def list_known_models() -> list[LLMKnownModel]:
    """Return the list of well-known models for each provider.

    This endpoint requires no authentication — used to populate the
    model picker when the user doesn't have an API key yet.

    Models are ordered: cheap tier first within each provider.
    """
    models: list[LLMKnownModel] = []
    for provider, model_list in KNOWN_MODELS.items():
        for entry in model_list:
            models.append(
                LLMKnownModel(
                    provider=provider,
                    model_id=entry["id"],
                    display_name=entry["name"],
                    tier=entry["tier"],
                )
            )
    return models
