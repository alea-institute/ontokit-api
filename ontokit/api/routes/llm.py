"""FastAPI routes for LLM configuration, usage, and provider metadata.

Routes:
  GET  /projects/{project_id}/llm/config       — project LLM config (any member)
  PUT  /projects/{project_id}/llm/config       — update LLM config (owner/admin only)
  POST /projects/{project_id}/llm/test-connection — test provider connectivity (owner/admin)
  GET  /projects/{project_id}/llm/usage        — usage dashboard (owner/admin only)
  GET  /projects/{project_id}/llm/status       — LLM availability/budget status (any member)
  PATCH /projects/{project_id}/members/{user_id}/flags — toggle member flags (owner/admin)
  GET  /llm/providers                          — static provider list (public)
  GET  /llm/known-models                       — static known-models list (public)

Authorization pattern mirrors embeddings.py:
- RequiredUser dependency for authenticated routes
- Role check via ProjectService._get_user_role or direct DB query
- Owner/admin check: role not in ("owner", "admin") → 403
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import uuid
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.core.auth import RequiredUser, require_authenticated_identity
from ontokit.core.database import get_db
from ontokit.models.llm_config import LLMAuditLog, ProjectLLMConfig
from ontokit.models.project import ProjectMember
from ontokit.schemas.llm import (
    LLMAuditEntry,
    LLMAuditHistoryResponse,
    LLMConfigResponse,
    LLMConfigUpdate,
    LLMKnownModel,
    LLMProviderInfo,
    LLMProviderType,
    LLMStatusResponse,
    LLMUsageResponse,
    MemberFlagsResponse,
    MemberFlagsUpdate,
)
from ontokit.services.llm import (
    PricingUnavailableError,
    check_llm_access,
    decrypt_secret,
    encrypt_secret,
    get_budget_status,
    get_model_pricing,
    get_provider,
    get_usage_summary,
    validate_base_url,
)
from ontokit.services.llm.audit import finalize_llm_call, reserve_llm_call
from ontokit.services.llm.rate_limiter import RATE_LIMITS
from ontokit.services.llm.registry import (
    KNOWN_MODELS,
    PROVIDER_DISPLAY_NAMES,
    PROVIDER_ICON_NAMES,
    PROVIDER_REQUIRES_KEY,
)
from ontokit.services.llm.ssrf import provider_allows_private_network

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

_AUDIT_CURSOR_KEYS = frozenset({"v", "project_id", "created_at", "id"})
_AUDIT_CURSOR_STRING_FIELDS = ("project_id", "created_at", "id")


# ── Helpers ───────────────────────────────────────────────────────────────────


def _encode_audit_cursor(row: LLMAuditLog) -> str:
    payload = {
        "v": 1,
        "project_id": str(row.project_id),
        "created_at": row.created_at.isoformat(),
        "id": str(row.id),
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode())
    return encoded.rstrip(b"=").decode()


def _decode_audit_cursor(cursor: str, project_id: UUID) -> tuple[datetime, UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.b64decode(padded, altchars=b"-_", validate=True))
        if not isinstance(payload, dict) or set(payload) != _AUDIT_CURSOR_KEYS:
            raise ValueError("unexpected cursor fields")
        if type(payload["v"]) is not int or payload["v"] != 1:
            raise ValueError("unsupported cursor version")
        if any(not isinstance(payload[field], str) for field in _AUDIT_CURSOR_STRING_FIELDS):
            raise ValueError("cursor fields must be strings")
        if UUID(payload["project_id"]) != project_id:
            raise ValueError("cursor scope mismatch")
        created_at = datetime.fromisoformat(payload["created_at"])
        if created_at.tzinfo is None:
            raise ValueError("cursor timestamp must be timezone-aware")
        row_id = UUID(payload["id"])
    except (
        binascii.Error,
        UnicodeDecodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Invalid audit cursor",
        ) from exc
    return created_at, row_id


async def _get_member_role(db: AsyncSession, project_id: UUID, user_id: str) -> str | None:
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
    # Explicit None check: a falsy-but-present role (e.g. "") must NOT
    # silently escalate to admin.
    return role if role is not None else "admin"  # superadmin fallback


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


async def _get_llm_config(db: AsyncSession, project_id: UUID) -> ProjectLLMConfig | None:
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


def _provider_connection_failure(provider: str, exc: Exception) -> dict[str, bool | str]:
    """Return a stable failure without echoing untrusted upstream details."""
    logger.warning(
        "LLM provider connection test failed: provider=%s error_type=%s",
        provider,
        type(exc).__name__,
    )
    return {"success": False, "error": "Provider connection failed"}


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

    config = await _get_llm_config(db, project_id)

    # Validate base_url if provided. The effective provider determines whether
    # private/local URLs are allowed; on a base_url-only update, fall back to the
    # stored provider so an existing local (e.g. Ollama) config isn't rejected.
    if data.base_url:
        effective_provider = data.provider or (LLMProviderType(config.provider) if config else None)
        allow_private = (
            provider_allows_private_network(effective_provider, data.base_url)
            if effective_provider
            else False
        )
        try:
            validate_base_url(data.base_url, allow_private=allow_private)
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid base_url: {e}",
            ) from e

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
) -> dict[str, bool | str]:
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

    # Re-validate base_url immediately before the outbound call. SSRF validation
    # at config-write time is not sufficient on its own: DNS can be rebound
    # between write and use (TOCTOU), so we re-resolve and re-check here — the
    # only outbound call this slice makes to a user-controlled endpoint. The
    # provider transport also pins each connection to its validated numeric IP
    # and disables redirects.
    if config.base_url:
        allow_private = provider_allows_private_network(config.provider, config.base_url)
        try:
            validate_base_url(config.base_url, allow_private=allow_private)
        except ValueError as exc:
            return {"success": False, "error": f"Invalid base_url: {exc}"}

    # Resolve API key: BYO header wins over stored key (BYO key is NEVER stored)
    if x_byo_api_key:
        api_key = x_byo_api_key  # ephemeral — never logged, never stored
    elif config.api_key_encrypted:
        api_key = decrypt_secret(config.api_key_encrypted)
    else:
        api_key = None

    # A model-backed connection test spends a minimal request. Reserve that
    # projected cost before the provider call so tests cannot bypass the same
    # project cap enforced for ordinary paid work. BYO tests remain audited but
    # are excluded from the project budget.
    is_byo_key = bool(x_byo_api_key)
    provider_is_local = config.provider in {provider.value for provider in _LOCAL_PROVIDERS}
    input_tokens = 1 if config.model else 0
    output_tokens = 1 if config.model else 0
    cost_estimate = 0.0
    if config.model and not provider_is_local:
        try:
            input_price, output_price = await get_model_pricing(config.model)
            cost_estimate = input_tokens * input_price + output_tokens * output_price
        except PricingUnavailableError:
            if not is_byo_key:
                return {
                    "success": False,
                    "error": "Pricing data is unavailable; connection test paused",
                }

    reservation_id, budget_reason = await reserve_llm_call(
        db,
        project_id=project_id,
        config=config,
        user_id=user.id,
        model=config.model or "",
        provider=config.provider,
        endpoint="llm/connection-test",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_estimate_usd=cost_estimate,
        is_byo_key=is_byo_key,
    )
    if reservation_id is None:
        return {
            "success": False,
            "error": (
                "Daily spending cap reached"
                if budget_reason == "daily_cap_reached"
                else "Monthly LLM budget exhausted"
            ),
        }

    succeeded = False
    try:
        provider = get_provider(
            provider_type=config.provider,
            api_key=api_key,
            base_url=config.base_url,
            model=config.model,
        )
        # 10-second timeout per spec
        await asyncio.wait_for(provider.test_connection(), timeout=10.0)
        succeeded = True
        response: dict[str, bool | str] = {"success": True}
    except TimeoutError:
        response = {"success": False, "error": "Connection timed out (10s limit)"}
    except Exception as exc:
        # Provider exceptions may echo response bodies, internal URLs, request
        # headers, or credentials. Keep the user-facing contract generic and
        # log only non-sensitive classification data.
        response = _provider_connection_failure(config.provider, exc)

    try:
        await finalize_llm_call(
            db,
            reservation_id,
            "llm/connection-test",
            succeeded=succeeded,
        )
    except Exception as exc:
        # The committed reservation still protects the cap and records an
        # indeterminate outcome. Do not turn a bookkeeping outage into an
        # automatic retry of provider work that has already happened.
        logger.error(
            "ALERT llm_audit_finalize_failed: project=%s provider=%s error_type=%s",
            project_id,
            config.provider,
            type(exc).__name__,
            extra={
                "event": "llm_audit_finalize_failed",
                "project_id": str(project_id),
                "provider": config.provider,
                "error_type": type(exc).__name__,
            },
        )
    return response


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


@router.get("/{project_id}/llm/audit", response_model=LLMAuditHistoryResponse)
async def get_llm_audit_history(
    project_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: RequiredUser,
    cursor: str | None = Query(default=None, max_length=1024),
    limit: int = Query(default=50, ge=1, le=100),
) -> LLMAuditHistoryResponse:
    """Return metadata-only per-call receipts for project owners and admins."""
    require_authenticated_identity(user)
    await _require_owner_or_admin(db, project_id, user.id, user.is_superadmin)

    query = select(LLMAuditLog).where(LLMAuditLog.project_id == project_id)
    if cursor is not None:
        created_at, row_id = _decode_audit_cursor(cursor, project_id)
        query = query.where(tuple_(LLMAuditLog.created_at, LLMAuditLog.id) < (created_at, row_id))
    query = query.order_by(LLMAuditLog.created_at.desc(), LLMAuditLog.id.desc()).limit(limit + 1)

    rows = list((await db.execute(query)).scalars().all())
    page = rows[:limit]
    next_cursor = _encode_audit_cursor(page[-1]) if len(rows) > limit and page else None
    return LLMAuditHistoryResponse(
        entries=[
            LLMAuditEntry(
                id=row.id,
                timestamp=row.created_at,
                user_id=row.user_id,
                model=row.model,
                provider=row.provider,
                endpoint=row.endpoint,
                input_tokens=row.input_tokens,
                output_tokens=row.output_tokens,
                cost_estimate_usd=row.cost_estimate_usd,
                is_byo_key=row.is_byo_key,
            )
            for row in page
        ],
        next_cursor=next_cursor,
    )


@router.get("/{project_id}/llm/status", response_model=LLMStatusResponse)
async def get_llm_status(
    project_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: RequiredUser,
) -> LLMStatusResponse:
    """Return LLM feature availability for the caller's project.

    Accessible to any project member. Combines provider configuration state
    with budget exhaustion and the caller's per-role daily allowance so the
    frontend can gate LLM affordances. Advisory only — the dispatch path
    (PR-5) re-checks budget and rate limits server-side on every call.
    """
    role = await _require_project_member(db, project_id, user.id, user.is_superadmin)
    config = await _get_llm_config(db, project_id)
    has_llm_access = check_llm_access(role)

    configured = False
    provider_type: LLMProviderType | None = None
    if config:
        try:
            provider_enum = LLMProviderType(config.provider)
        except ValueError:
            # Legacy/unknown provider string: treat as unconfigured rather
            # than 500. (The config route shares this pattern — follow-up.)
            provider_enum = None
        if provider_enum is not None:
            provider_type = provider_enum
            is_local = provider_enum in _LOCAL_PROVIDERS
            # Local providers (Ollama etc.) don't need an API key to be usable
            configured = bool(config.model) and (bool(config.api_key_encrypted) or is_local)

    budget_exhausted = False
    monthly_spent_usd = 0.0
    monthly_budget_usd: float | None = None
    burn_rate_daily = 0.0
    if config:
        budget_status = await get_budget_status(db, project_id, config)
        budget_exhausted = budget_status["budget_exhausted"]
        monthly_budget_usd = budget_status["monthly_budget_usd"]
        # Spend telemetry stays within the sensitivity line the rest of the
        # module draws: /llm/usage is owner/admin-only, /llm/config (caps) is
        # member-readable. No-access roles (viewer) get the gating booleans
        # and the cap, but not actual spend/burn numbers.
        if has_llm_access:
            monthly_spent_usd = budget_status["monthly_spent_usd"]
            burn_rate_daily = budget_status["burn_rate_daily_usd"]

    # Per-role allowance. RATE_LIMITS encodes access directly: 0 = no LLM
    # access (viewer/unknown roles), None = unlimited (owner/admin) — a
    # no-access role must report 0, since null reads as "uncapped". For capped
    # roles this is the static cap, not a live count: Redis-backed remaining
    # counts arrive with the dispatch layer (PR-5), which injects Redis here.
    # This is a pure static per-role lookup — independent of `configured`, so
    # a no-access role reports 0 even on an unconfigured project (the null-here
    # = uncapped invariant must not depend on config state).
    daily_remaining: int | None = RATE_LIMITS.get(role, 0)

    return LLMStatusResponse(
        configured=configured,
        provider=provider_type,
        budget_exhausted=budget_exhausted,
        daily_remaining=daily_remaining,
        monthly_budget_usd=monthly_budget_usd,
        monthly_spent_usd=monthly_spent_usd,
        burn_rate_daily_usd=burn_rate_daily,
    )


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

    Currently supports `can_self_merge_structural` (ROLE-03): a per-editor
    override allowing structural PR self-merge.
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

    # Privilege changes must leave a trace (ROLE-03 grants structural
    # self-merge). Metadata only — mirrors the LLM audit-log posture.
    logger.info(
        "member flags updated: project=%s actor=%s target=%s can_self_merge_structural=%s",
        project_id,
        user.id,
        target_user_id,
        data.can_self_merge_structural,
    )

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
