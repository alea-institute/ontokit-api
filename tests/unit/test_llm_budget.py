"""Tests for monthly/daily budget enforcement — COST-01/COST-02, D-17.

check_budget order: daily sub-cap BEFORE monthly budget (Open Question 3).
BYO-key calls are excluded from budget aggregation (D-17) — asserted against
the compiled SQL, not just the mocked result.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.dialects import postgresql

from ontokit.models.llm_config import ProjectLLMConfig
from ontokit.services.llm.budget import (
    check_budget,
    get_budget_status,
    get_daily_spend,
    get_monthly_spend,
    lock_and_check_budget,
    project_budget_lock_key,
)


def _config(monthly: float | None = None, daily: float | None = None) -> ProjectLLMConfig:
    config = Mock(spec=ProjectLLMConfig)
    config.monthly_budget_usd = monthly
    config.daily_cap_usd = daily
    return config


def _db(scalars: list[float]) -> AsyncMock:
    """Mock AsyncSession whose successive execute() calls yield the given SUM scalars."""
    db = AsyncMock()
    results = []
    for value in scalars:
        result = Mock()
        result.scalar_one = Mock(return_value=value)
        results.append(result)
    db.execute = AsyncMock(side_effect=results)
    return db


def _db_status(monthly: float, daily: float, week_total: float) -> AsyncMock:
    """Mock AsyncSession for get_budget_status's single consolidated round-trip.

    get_budget_status issues ONE query with three FILTER'd SUM columns and reads
    result.one().monthly / .daily / .week (week = 7-day spend total; burn rate is
    week_total / 7).
    """
    db = AsyncMock()
    row = Mock()
    row.monthly = monthly
    row.daily = daily
    row.week = week_total
    result = Mock()
    result.one = Mock(return_value=row)
    db.execute = AsyncMock(return_value=result)
    return db


@pytest.mark.asyncio
async def test_monthly_spend_coerces_null_sum_to_zero():
    db = _db([0.0])
    assert await get_monthly_spend(db, "p") == 0.0


@pytest.mark.asyncio
async def test_spend_queries_exclude_byo_key_calls():
    """D-17: only project-key calls (is_byo_key = false) count against budget."""
    project_uuid = uuid.UUID("11111111-1111-1111-1111-111111111111")
    for fn in (get_monthly_spend, get_daily_spend):
        db = _db([1.0])
        await fn(db, project_uuid)  # type: ignore[arg-type]  # UUID renders in literal SQL
        query = db.execute.await_args.args[0]
        sql = str(query.compile(compile_kwargs={"literal_binds": True}))
        assert "is_byo_key" in sql and "false" in sql.lower()
        # Time window is UTC-anchored (mirrors the audit.py timestamptz fix)
        assert "date_trunc" in sql and "UTC" in sql


@pytest.mark.asyncio
async def test_no_budget_configured_is_unlimited():
    db = _db([])
    allowed, reason = await check_budget(db, "p", _config(monthly=None, daily=None))
    assert (allowed, reason) == (True, None)
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_daily_cap_checked_before_monthly():
    """Open Question 3: daily sub-cap fires first even when monthly is also blown."""
    db = _db([10.0])  # daily spend >= cap → short-circuits before monthly query
    allowed, reason = await check_budget(db, "p", _config(monthly=5.0, daily=10.0))
    assert (allowed, reason) == (False, "daily_cap_reached")
    assert db.execute.await_count == 1


@pytest.mark.asyncio
async def test_monthly_budget_exhausted():
    db = _db([100.0])
    allowed, reason = await check_budget(db, "p", _config(monthly=100.0, daily=None))
    assert (allowed, reason) == (False, "budget_exhausted")


@pytest.mark.asyncio
async def test_within_both_limits_allowed():
    db = _db([2.0, 50.0])  # daily spend, then monthly spend
    allowed, reason = await check_budget(db, "p", _config(monthly=100.0, daily=10.0))
    assert (allowed, reason) == (True, None)


@pytest.mark.asyncio
async def test_projected_cost_cannot_cross_monthly_budget():
    db = _db([9.5])
    allowed, reason = await check_budget(
        db,
        uuid.UUID("11111111-1111-1111-1111-111111111111"),
        _config(monthly=10.0, daily=None),
        additional_cost_usd=0.51,
    )
    assert (allowed, reason) == (False, "budget_exhausted")


@pytest.mark.asyncio
async def test_budget_reservation_locks_before_reading_spend():
    project_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
    db = _db([0.0, 1.0])

    allowed, reason = await lock_and_check_budget(
        db,
        project_id,
        _config(monthly=10.0, daily=None),
        additional_cost_usd=1.0,
    )

    assert (allowed, reason) == (True, None)
    assert db.execute.await_count == 2
    lock_query = db.execute.await_args_list[0].args[0]
    compiled = lock_query.compile(compile_kwargs={"literal_binds": True})
    assert "pg_advisory_xact_lock" in str(compiled)
    assert str(project_budget_lock_key(project_id)) in str(compiled)


@pytest.mark.asyncio
async def test_budget_status_snapshot():
    # monthly spend, daily spend, 7d burn total basis (burn_rate = 3.5 / 7 = 0.5)
    db = _db_status(monthly=80.0, daily=5.0, week_total=3.5)
    status = await get_budget_status(db, "p", _config(monthly=100.0, daily=10.0))
    assert status["monthly_spent_usd"] == 80.0
    assert status["monthly_budget_usd"] == 100.0
    assert status["budget_consumed_pct"] == pytest.approx(80.0)  # 0–100 scale (matches usage route)
    assert status["budget_exhausted"] is False
    assert status["daily_spent_usd"] == 5.0
    assert status["daily_cap_usd"] == 10.0
    assert status["burn_rate_daily_usd"] == pytest.approx(0.5)
    # Consolidated into a single round-trip (member-polled endpoint).
    assert db.execute.await_count == 1


@pytest.mark.asyncio
async def test_budget_status_exhausted_via_daily_cap():
    db = _db_status(monthly=10.0, daily=10.0, week_total=1.0)
    status = await get_budget_status(db, "p", _config(monthly=100.0, daily=10.0))
    assert status["budget_exhausted"] is True


@pytest.mark.asyncio
async def test_budget_status_exhausted_via_monthly():
    db = _db_status(monthly=100.0, daily=0.0, week_total=1.0)
    status = await get_budget_status(db, "p", _config(monthly=100.0, daily=None))
    assert status["budget_exhausted"] is True


@pytest.mark.asyncio
async def test_budget_status_unlimited_project_never_exhausts():
    db = _db_status(monthly=5000.0, daily=500.0, week_total=70.0)
    status = await get_budget_status(db, "p", _config(monthly=None, daily=None))
    assert status["budget_exhausted"] is False
    assert status["budget_consumed_pct"] == 0.0
    assert status["monthly_budget_usd"] is None


@pytest.mark.asyncio
async def test_budget_status_query_excludes_byo_and_bounds_utc_windows():
    """Pin the correctness-sensitive parts of the consolidated single-query
    get_budget_status against the compiled SQL (mocked results can't see these):
    - BYO-key calls excluded (is_byo_key = false, D-17)
    - all three windows are UTC-anchored (date_trunc(..., 'UTC'))
    - the outer row bound uses LEAST(month_start, week_ago) so no FILTER window
      is under-covered (burn-rate 7d can predate the month start).
    """
    project_uuid = uuid.UUID("22222222-2222-2222-2222-222222222222")
    db = _db_status(monthly=1.0, daily=1.0, week_total=1.0)
    await get_budget_status(db, project_uuid, _config(monthly=100.0, daily=10.0))
    assert db.execute.await_count == 1
    # Postgres dialect (not literal_binds — the 7d timedelta can't literal-render).
    compiled = db.execute.await_args.args[0].compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "is_byo_key" in sql and "false" in sql.lower()  # D-17 BYO exclusion
    assert "date_trunc" in sql  # UTC-anchored month/day windows
    assert "UTC" in compiled.params.values()  # tz arg is a bound param
    assert "least" in sql.lower()  # outer bound covers the widest (7d) window
    # Three FILTER'd aggregates in one statement (month / day / week).
    assert sql.upper().count("FILTER") == 3
