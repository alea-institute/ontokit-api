"""Audit log writer and usage aggregation for LLM calls.

Per D-08: Only metadata is stored — no prompt or response content.
Per LLM-07: Every project-key LLM call creates an audit log entry.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.models.llm_config import LLMAuditLog
from ontokit.schemas.llm import LLMUsageResponse, LLMUserUsage
from ontokit.services.llm.budget import BudgetConfig, lock_and_check_budget

logger = logging.getLogger(__name__)

_RESERVED_SUFFIX = ":reserved"
_FAILED_SUFFIX = ":failed"
_MAX_ENDPOINT_LENGTH = 200


def _reservation_endpoint(endpoint: str, suffix: str) -> str:
    return f"{endpoint[: _MAX_ENDPOINT_LENGTH - len(suffix)]}{suffix}"


async def log_llm_call(
    db: AsyncSession,
    project_id: str | None,
    user_id: str,
    model: str,
    provider: str,
    endpoint: str,
    input_tokens: int,
    output_tokens: int,
    cost_estimate_usd: float,
    is_byo_key: bool = False,
) -> LLMAuditLog:
    """Write an audit log entry for an LLM call.

    Stores metadata only — no prompt or response content (per D-08, privacy-safe).

    Args:
        db: Async SQLAlchemy session.
        project_id: UUID string for the project, or None for instance-level
            calls with no owning project (KTD20 — e.g. PR Party brief
            generation). Null-project rows are excluded from every per-project
            budget query, which filters on ``project_id``.
        user_id: The authenticated user ID.
        model: Model identifier used (e.g. "gpt-4o").
        provider: Provider name (e.g. "openai").
        endpoint: Endpoint path that triggered the call (e.g. "llm/generate-suggestions").
        input_tokens: Number of input tokens consumed.
        output_tokens: Number of output tokens produced.
        cost_estimate_usd: Estimated cost for this call in USD.
        is_byo_key: True if the user supplied their own API key (not billed to project).

    Returns:
        The newly created LLMAuditLog instance (already flushed, not yet committed).
    """
    entry = LLMAuditLog(
        id=uuid.uuid4(),
        project_id=project_id,
        user_id=user_id,
        model=model,
        provider=provider,
        endpoint=endpoint,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_estimate_usd=cost_estimate_usd,
        is_byo_key=is_byo_key,
    )
    db.add(entry)
    await db.flush()
    return entry


async def reserve_llm_call(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    config: BudgetConfig,
    user_id: str,
    model: str,
    provider: str,
    endpoint: str,
    input_tokens: int,
    output_tokens: int,
    cost_estimate_usd: float,
    is_byo_key: bool = False,
) -> tuple[uuid.UUID | None, str | None]:
    """Atomically reserve budget and persist an indeterminate call receipt.

    Committing the receipt releases the per-project transaction lock before the
    network call. A crash or cancellation therefore leaves a conservative
    ``:reserved`` record instead of an invisible charge and retry window.
    """
    if not is_byo_key:
        within_budget, reason = await lock_and_check_budget(
            db,
            project_id,
            config,
            additional_cost_usd=cost_estimate_usd,
        )
        if not within_budget:
            await db.rollback()
            return None, reason

    entry = await log_llm_call(
        db=db,
        project_id=str(project_id),
        user_id=user_id,
        model=model,
        provider=provider,
        endpoint=_reservation_endpoint(endpoint, _RESERVED_SUFFIX),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_estimate_usd=cost_estimate_usd,
        is_byo_key=is_byo_key,
    )
    await db.commit()
    return entry.id, None


async def finalize_llm_call(
    db: AsyncSession,
    reservation_id: uuid.UUID,
    endpoint: str,
    *,
    succeeded: bool,
) -> None:
    """Mark a committed reservation successful or failed without losing spend."""
    final_endpoint = endpoint if succeeded else _reservation_endpoint(endpoint, _FAILED_SUFFIX)
    await db.execute(
        update(LLMAuditLog)
        .where(LLMAuditLog.id == reservation_id)
        .values(endpoint=final_endpoint)
    )
    await db.commit()


async def get_usage_summary(db: AsyncSession, project_id: str) -> LLMUsageResponse:
    """Aggregate LLM usage for the current month, broken down by user.

    Returns an LLMUsageResponse with:
    - total_calls and total_cost_usd for the current calendar month
    - Per-user breakdown: calls_today, calls_this_month, cost, is_byo_key
    - burn_rate_daily_usd: average daily cost over the last 7 days

    Note: Aggregates all calls (BYO and project-key) for reporting purposes.
    Budget percentage only counts non-BYO calls.

    Args:
        db: Async SQLAlchemy session.
        project_id: UUID string for the project.
    """
    # ── Month-level totals ────────────────────────────────────────────────────
    month_result = await db.execute(
        select(
            func.coalesce(func.count(LLMAuditLog.id), 0).label("total_calls"),
            func.coalesce(func.sum(LLMAuditLog.cost_estimate_usd), 0.0).label("total_cost"),
        )
        .where(LLMAuditLog.project_id == project_id)
        .where(LLMAuditLog.created_at >= func.date_trunc("month", func.now(), "UTC"))
    )
    month_row = month_result.one()
    total_calls: int = int(month_row.total_calls)
    total_cost_usd: float = float(month_row.total_cost)

    # ── Burn rate: avg daily cost over last 7 days (non-BYO only) ─────────────
    burn_result = await db.execute(
        select(func.coalesce(func.sum(LLMAuditLog.cost_estimate_usd), 0.0))
        .where(LLMAuditLog.project_id == project_id)
        .where(LLMAuditLog.is_byo_key.is_(False))
        .where(LLMAuditLog.created_at >= func.now() - timedelta(days=7))
    )
    burn_7d: float = float(burn_result.scalar_one())
    burn_rate_daily = round(burn_7d / 7.0, 6)

    # ── Per-user breakdown ────────────────────────────────────────────────────
    # calls this month
    per_user_month = await db.execute(
        select(
            LLMAuditLog.user_id,
            func.count(LLMAuditLog.id).label("calls_month"),
            func.coalesce(func.sum(LLMAuditLog.cost_estimate_usd), 0.0).label("cost_month"),
            func.bool_or(LLMAuditLog.is_byo_key).label("any_byo"),
        )
        .where(LLMAuditLog.project_id == project_id)
        .where(LLMAuditLog.created_at >= func.date_trunc("month", func.now(), "UTC"))
        .group_by(LLMAuditLog.user_id)
    )
    month_rows = {r.user_id: r for r in per_user_month.all()}

    # calls today
    per_user_today = await db.execute(
        select(
            LLMAuditLog.user_id,
            func.count(LLMAuditLog.id).label("calls_today"),
        )
        .where(LLMAuditLog.project_id == project_id)
        .where(LLMAuditLog.created_at >= func.date_trunc("day", func.now(), "UTC"))
        .group_by(LLMAuditLog.user_id)
    )
    today_rows = {r.user_id: int(r.calls_today) for r in per_user_today.all()}

    users: list[LLMUserUsage] = []
    for uid, mrow in month_rows.items():
        users.append(
            LLMUserUsage(
                user_id=uid,
                user_name=None,  # Name lookup deferred to the route layer
                calls_today=today_rows.get(uid, 0),
                calls_this_month=int(mrow.calls_month),
                cost_this_month_usd=float(mrow.cost_month),
                is_byo_key=bool(mrow.any_byo),
            )
        )

    # budget_consumed_pct is computed in the route with config context (we return 0.0 here)
    return LLMUsageResponse(
        total_calls=total_calls,
        total_cost_usd=total_cost_usd,
        budget_consumed_pct=0.0,  # caller must patch with config context
        burn_rate_daily_usd=burn_rate_daily,
        users=users,
    )
