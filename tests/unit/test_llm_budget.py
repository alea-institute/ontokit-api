"""Tests for monthly/daily budget enforcement — COST-01/COST-02, D-17.

check_budget order: daily sub-cap BEFORE monthly budget (Open Question 3).
BYO-key calls are excluded from budget aggregation (D-17) — asserted against
the compiled SQL, not just the mocked result.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, Mock

import pytest

from ontokit.models.llm_config import ProjectLLMConfig
from ontokit.services.llm.budget import (
    check_budget,
    get_budget_status,
    get_daily_spend,
    get_monthly_spend,
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
async def test_budget_status_snapshot():
    # monthly spend, daily spend, 7d burn total basis
    db = _db([80.0, 5.0, 3.5])
    status = await get_budget_status(db, "p", _config(monthly=100.0, daily=10.0))
    assert status["monthly_spent_usd"] == 80.0
    assert status["monthly_budget_usd"] == 100.0
    assert status["budget_consumed_pct"] == pytest.approx(0.8)
    assert status["budget_exhausted"] is False
    assert status["daily_spent_usd"] == 5.0
    assert status["daily_cap_usd"] == 10.0


@pytest.mark.asyncio
async def test_budget_status_exhausted_via_daily_cap():
    db = _db([10.0, 10.0, 1.0])
    status = await get_budget_status(db, "p", _config(monthly=100.0, daily=10.0))
    assert status["budget_exhausted"] is True


@pytest.mark.asyncio
async def test_budget_status_exhausted_via_monthly():
    db = _db([100.0, 0.0, 1.0])
    status = await get_budget_status(db, "p", _config(monthly=100.0, daily=None))
    assert status["budget_exhausted"] is True


@pytest.mark.asyncio
async def test_budget_status_unlimited_project_never_exhausts():
    db = _db([5000.0, 500.0, 70.0])
    status = await get_budget_status(db, "p", _config(monthly=None, daily=None))
    assert status["budget_exhausted"] is False
    assert status["budget_consumed_pct"] == 0.0
    assert status["monthly_budget_usd"] is None
