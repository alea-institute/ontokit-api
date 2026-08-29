"""Monthly budget enforcement for project-key LLM calls.

Per D-17: BYO-key calls do NOT count against the project budget.
Per COST-01/COST-02: budget exhaustion returns advisory state (enforced at dispatch time).
Per Pitfall 5 (RESEARCH.md): SUM() returns NULL when no rows — coerce to 0.0.

Open Question 3 (RESEARCH.md): daily sub-cap is checked BEFORE monthly budget.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from hashlib import blake2b
from typing import Protocol, TypedDict

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.models.llm_config import LLMAuditLog, ProjectLLMConfig

logger = logging.getLogger(__name__)


class BudgetStatus(TypedDict):
    """Full budget snapshot for a project, as returned by get_budget_status()."""

    monthly_spent_usd: float
    monthly_budget_usd: float | None
    budget_consumed_pct: float
    burn_rate_daily_usd: float
    budget_exhausted: bool
    daily_spent_usd: float
    daily_cap_usd: float | None


class BudgetConfig(Protocol):
    """The cap fields shared by generation and embedding configurations."""

    monthly_budget_usd: float | None
    daily_cap_usd: float | None


@dataclass
class BudgetLimits:
    """Session-independent snapshot of the two project spend caps."""

    monthly_budget_usd: float | None
    daily_cap_usd: float | None


async def get_monthly_spend(db: AsyncSession, project_id: uuid.UUID) -> float:
    """Return total non-BYO LLM spend for the current calendar month (UTC).

    Only project-key calls (is_byo_key=False) count against the budget.

    Args:
        db: Async SQLAlchemy session.
        project_id: The project UUID.

    Returns:
        Total cost in USD as a float; 0.0 if no calls logged yet.
    """
    result = await db.execute(
        select(func.coalesce(func.sum(LLMAuditLog.cost_estimate_usd), 0.0))
        .where(LLMAuditLog.project_id == project_id)
        .where(LLMAuditLog.is_byo_key.is_(False))
        .where(LLMAuditLog.created_at >= func.date_trunc("month", func.now(), "UTC"))
    )
    value: float = result.scalar_one()
    return float(value)


async def get_daily_spend(db: AsyncSession, project_id: uuid.UUID) -> float:
    """Return total non-BYO LLM spend for the current calendar day (UTC).

    Args:
        db: Async SQLAlchemy session.
        project_id: The project UUID.

    Returns:
        Total cost in USD as a float; 0.0 if no calls logged yet.
    """
    result = await db.execute(
        select(func.coalesce(func.sum(LLMAuditLog.cost_estimate_usd), 0.0))
        .where(LLMAuditLog.project_id == project_id)
        .where(LLMAuditLog.is_byo_key.is_(False))
        .where(LLMAuditLog.created_at >= func.date_trunc("day", func.now(), "UTC"))
    )
    value: float = result.scalar_one()
    return float(value)


async def check_budget(
    db: AsyncSession,
    project_id: uuid.UUID,
    config: BudgetConfig,
    *,
    additional_cost_usd: float = 0.0,
) -> tuple[bool, str | None]:
    """Check whether the project is within its budget limits.

    Checks the daily sub-cap first, then the monthly budget.

    Args:
        db: Async SQLAlchemy session.
        project_id: The project UUID.
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
        if daily_spend >= config.daily_cap_usd or (
            additional_cost_usd > 0 and daily_spend + additional_cost_usd > config.daily_cap_usd
        ):
            return (False, "daily_cap_reached")

    # Check monthly budget
    if config.monthly_budget_usd is not None:
        monthly_spend = await get_monthly_spend(db, project_id)
        if monthly_spend >= config.monthly_budget_usd or (
            additional_cost_usd > 0
            and monthly_spend + additional_cost_usd > config.monthly_budget_usd
        ):
            return (False, "budget_exhausted")

    return (True, None)


def project_budget_lock_key(project_id: uuid.UUID) -> int:
    """Return the stable signed bigint used for a project's budget lock."""
    digest = blake2b(
        project_id.bytes,
        digest_size=8,
        person=b"ontokit-budget",
    ).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


async def lock_and_check_budget(
    db: AsyncSession,
    project_id: uuid.UUID,
    config: BudgetConfig,
    *,
    additional_cost_usd: float,
) -> tuple[bool, str | None]:
    """Serialize a projected budget check inside the caller's transaction.

    The caller must write and commit its reservation using the same session
    before starting provider work. PostgreSQL releases this transaction-scoped
    advisory lock on commit or rollback.
    """
    await db.execute(select(func.pg_advisory_xact_lock(project_budget_lock_key(project_id))))
    return await check_budget(
        db,
        project_id,
        config,
        additional_cost_usd=additional_cost_usd,
    )


async def get_budget_status(
    db: AsyncSession,
    project_id: uuid.UUID,
    config: ProjectLLMConfig,
) -> BudgetStatus:
    """Return a full budget status snapshot for the project.

    Returns:
        BudgetStatus with monthly/daily spend, budget caps, consumed pct,
        burn rate, and the overall budget_exhausted flag.
    """
    # One round-trip instead of three: this backs the member-reachable
    # /llm/status route that every project member's frontend polls. All three
    # windows share the (project_id, created_at) index; FILTER does the rest.
    month_start = func.date_trunc("month", func.now(), "UTC")
    day_start = func.date_trunc("day", func.now(), "UTC")
    week_ago = func.now() - timedelta(days=7)
    result = await db.execute(
        select(
            func.coalesce(
                func.sum(LLMAuditLog.cost_estimate_usd).filter(
                    LLMAuditLog.created_at >= month_start
                ),
                0.0,
            ).label("monthly"),
            func.coalesce(
                func.sum(LLMAuditLog.cost_estimate_usd).filter(LLMAuditLog.created_at >= day_start),
                0.0,
            ).label("daily"),
            func.coalesce(
                func.sum(LLMAuditLog.cost_estimate_usd).filter(LLMAuditLog.created_at >= week_ago),
                0.0,
            ).label("week"),
        )
        .where(LLMAuditLog.project_id == project_id)
        .where(LLMAuditLog.is_byo_key.is_(False))
        .where(LLMAuditLog.created_at >= func.least(month_start, week_ago))
    )
    row = result.one()
    monthly_spent = float(row.monthly)
    daily_spent = float(row.daily)
    burn_rate = round(float(row.week) / 7.0, 6)

    # Percentage on a 0–100 scale, matching LLMUsageResponse.budget_consumed_pct
    # and get_llm_usage (routes/llm.py) — both use the *100 convention. Keeping
    # one scale across the two paths avoids a 100x mis-render in the first
    # consumer that reads this snapshot (e.g. a "budget %" banner or PR-5).
    budget_consumed_pct = 0.0
    if config.monthly_budget_usd and config.monthly_budget_usd > 0:
        budget_consumed_pct = round(monthly_spent / config.monthly_budget_usd * 100, 2)

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
