"""Monthly budget enforcement for project-key LLM calls.

Per D-17: BYO-key calls do NOT count against the project budget.
Per COST-01/COST-02: budget exhaustion returns advisory state (enforced at dispatch time).
Per Pitfall 5 (RESEARCH.md): SUM() returns NULL when no rows — coerce to 0.0.

Open Question 3 (RESEARCH.md): daily sub-cap is checked BEFORE monthly budget.
"""

from __future__ import annotations

import logging
from datetime import date

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.models.llm_config import LLMAuditLog, ProjectLLMConfig

logger = logging.getLogger(__name__)


async def get_monthly_spend(db: AsyncSession, project_id: str) -> float:
    """Return total non-BYO LLM spend for the current calendar month (UTC).

    Only project-key calls (is_byo_key=False) count against the budget.

    Args:
        db: Async SQLAlchemy session.
        project_id: UUID string for the project.

    Returns:
        Total cost in USD as a float; 0.0 if no calls logged yet.
    """
    result = await db.execute(
        select(func.coalesce(func.sum(LLMAuditLog.cost_estimate_usd), 0.0))
        .where(LLMAuditLog.project_id == project_id)
        .where(LLMAuditLog.is_byo_key.is_(False))
        .where(
            LLMAuditLog.created_at >= text("date_trunc('month', NOW() AT TIME ZONE 'UTC')")
        )
    )
    value: float = result.scalar_one()
    return float(value)


async def get_daily_spend(db: AsyncSession, project_id: str) -> float:
    """Return total non-BYO LLM spend for the current calendar day (UTC).

    Args:
        db: Async SQLAlchemy session.
        project_id: UUID string for the project.

    Returns:
        Total cost in USD as a float; 0.0 if no calls logged yet.
    """
    result = await db.execute(
        select(func.coalesce(func.sum(LLMAuditLog.cost_estimate_usd), 0.0))
        .where(LLMAuditLog.project_id == project_id)
        .where(LLMAuditLog.is_byo_key.is_(False))
        .where(
            LLMAuditLog.created_at >= text("date_trunc('day', NOW() AT TIME ZONE 'UTC')")
        )
    )
    value: float = result.scalar_one()
    return float(value)


async def _get_burn_rate_daily(db: AsyncSession, project_id: str) -> float:
    """Return average daily non-BYO spend over the last 7 days."""
    result = await db.execute(
        select(func.coalesce(func.sum(LLMAuditLog.cost_estimate_usd), 0.0))
        .where(LLMAuditLog.project_id == project_id)
        .where(LLMAuditLog.is_byo_key.is_(False))
        .where(
            LLMAuditLog.created_at >= text(
                "(NOW() AT TIME ZONE 'UTC') - INTERVAL '7 days'"
            )
        )
    )
    total_7d: float = float(result.scalar_one())
    return round(total_7d / 7.0, 6)


async def check_budget(
    db: AsyncSession,
    project_id: str,
    config: ProjectLLMConfig,
) -> tuple[bool, str | None]:
    """Check whether the project is within its budget limits.

    Checks the daily sub-cap first, then the monthly budget.

    Args:
        db: Async SQLAlchemy session.
        project_id: UUID string for the project.
        config: The project's LLMConfig row (may have monthly_budget_usd, daily_cap_usd).

    Returns:
        (True, None) if within limits or no budget set.
        (False, "daily_cap_reached") if today's spend has hit the daily cap.
        (False, "budget_exhausted") if this month's spend has hit the monthly budget.
    """
    # No budget configured — unlimited
    if config.monthly_budget_usd is None and config.daily_cap_usd is None:
        return (True, None)

    # Check daily sub-cap first (Open Question 3)
    if config.daily_cap_usd is not None:
        daily_spend = await get_daily_spend(db, project_id)
        if daily_spend >= config.daily_cap_usd:
            return (False, "daily_cap_reached")

    # Check monthly budget
    if config.monthly_budget_usd is not None:
        monthly_spend = await get_monthly_spend(db, project_id)
        if monthly_spend >= config.monthly_budget_usd:
            return (False, "budget_exhausted")

    return (True, None)


async def get_budget_status(
    db: AsyncSession,
    project_id: str,
    config: ProjectLLMConfig,
) -> dict:
    """Return a full budget status dict for the project.

    Returns:
        Dict with keys: monthly_spent_usd, monthly_budget_usd, budget_consumed_pct,
        burn_rate_daily_usd, budget_exhausted, daily_spent_usd, daily_cap_usd.
    """
    monthly_spent = await get_monthly_spend(db, project_id)
    daily_spent = await get_daily_spend(db, project_id)
    burn_rate = await _get_burn_rate_daily(db, project_id)

    budget_consumed_pct = 0.0
    if config.monthly_budget_usd and config.monthly_budget_usd > 0:
        budget_consumed_pct = monthly_spent / config.monthly_budget_usd

    budget_exhausted = False
    if config.daily_cap_usd is not None and daily_spent >= config.daily_cap_usd:
        budget_exhausted = True
    if config.monthly_budget_usd is not None and monthly_spent >= config.monthly_budget_usd:
        budget_exhausted = True

    return {
        "monthly_spent_usd": monthly_spent,
        "monthly_budget_usd": config.monthly_budget_usd,
        "budget_consumed_pct": budget_consumed_pct,
        "burn_rate_daily_usd": burn_rate,
        "budget_exhausted": budget_exhausted,
        "daily_spent_usd": daily_spent,
        "daily_cap_usd": config.daily_cap_usd,
    }
