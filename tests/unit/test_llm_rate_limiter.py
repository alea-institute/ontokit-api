"""Tests for Redis-based daily LLM rate limiting — COST-03/COST-04.

The limiter enforces per-role daily caps keyed per project-user-day (UTC).
Redis failures fail OPEN by design (documented trade-off: availability over
strict metering) — that behavior is pinned here so a silent change is caught.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from ontokit.services.llm.rate_limiter import (
    RATE_LIMITS,
    _rate_key,
    check_rate_limit,
    get_remaining_calls,
)


def _redis(incr: int = 1, get: bytes | None = None) -> AsyncMock:
    redis = AsyncMock()
    redis.incr = AsyncMock(return_value=incr)
    redis.expire = AsyncMock(return_value=True)
    redis.get = AsyncMock(return_value=get)
    return redis


def test_rate_limits_match_cost_requirements():
    """COST-03/COST-04: editor 500/day, suggester 100/day; owner/admin unlimited; viewer 0."""
    assert RATE_LIMITS == {
        "owner": None,
        "admin": None,
        "editor": 500,
        "suggester": 100,
        "viewer": 0,
    }


def test_rate_key_is_scoped_per_project_user_day():
    key = _rate_key("proj-1", "user-1", today="2026-07-07")
    assert key == "llm:rate:proj-1:user-1:2026-07-07"
    # Different project/user must never share a counter
    assert _rate_key("proj-2", "user-1", today="2026-07-07") != key
    assert _rate_key("proj-1", "user-2", today="2026-07-07") != key


@pytest.mark.asyncio
async def test_viewer_is_blocked_without_touching_redis():
    redis = _redis()
    assert await check_rate_limit(redis, "p", "u", "viewer") is False
    redis.incr.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_role_is_blocked():
    redis = _redis()
    assert await check_rate_limit(redis, "p", "u", "not-a-role") is False
    redis.incr.assert_not_called()


@pytest.mark.asyncio
async def test_owner_and_admin_are_unlimited_without_redis():
    redis = _redis()
    assert await check_rate_limit(redis, "p", "u", "owner") is True
    assert await check_rate_limit(redis, "p", "u", "admin") is True
    redis.incr.assert_not_called()


@pytest.mark.asyncio
async def test_editor_within_limit_allowed_and_ttl_set():
    redis = _redis(incr=1)
    assert await check_rate_limit(redis, "p", "u", "editor") is True
    redis.incr.assert_awaited_once()
    # TTL set with NX so a key left TTL-less by a crash is repaired
    redis.expire.assert_awaited_once()
    assert redis.expire.await_args.kwargs.get("nx") is True
    assert redis.expire.await_args.args[1] == 86400


@pytest.mark.asyncio
async def test_editor_at_exact_limit_still_allowed():
    redis = _redis(incr=500)
    assert await check_rate_limit(redis, "p", "u", "editor") is True


@pytest.mark.asyncio
async def test_editor_over_limit_blocked():
    redis = _redis(incr=501)
    assert await check_rate_limit(redis, "p", "u", "editor") is False


@pytest.mark.asyncio
async def test_suggester_over_limit_blocked():
    redis = _redis(incr=101)
    assert await check_rate_limit(redis, "p", "u", "suggester") is False


@pytest.mark.asyncio
async def test_redis_failure_fails_open_by_design(caplog):
    """Documented trade-off: Redis downtime must not block legitimate users.

    This is a metering-bypass vector (see PR body); pinned so any change to
    fail-closed is a conscious decision, not an accident. The fail-open path
    MUST emit the actionable alert marker so ops can page on it.
    """
    import logging

    from ontokit.services.llm.rate_limiter import FAIL_OPEN_EVENT

    redis = AsyncMock()
    redis.incr = AsyncMock(side_effect=ConnectionError("redis down"))
    with caplog.at_level(logging.WARNING, logger="ontokit.services.llm.rate_limiter"):
        assert await check_rate_limit(redis, "p", "u", "editor") is True

    records = [r for r in caplog.records if getattr(r, "event", None) == FAIL_OPEN_EVENT]
    assert len(records) == 1
    assert records[0].operation == "check_rate_limit"
    assert records[0].project_id == "p"
    assert records[0].levelno == logging.WARNING


@pytest.mark.asyncio
async def test_remaining_calls_matrix():
    # Unlimited roles report None
    assert await get_remaining_calls(_redis(), "p", "u", "owner") is None
    # Blocked roles report 0
    assert await get_remaining_calls(_redis(), "p", "u", "viewer") == 0
    # No calls yet: full limit
    assert await get_remaining_calls(_redis(get=None), "p", "u", "editor") == 500
    # Partially consumed
    assert await get_remaining_calls(_redis(get=b"120"), "p", "u", "editor") == 380
    # Overshoot clamps to 0, never negative
    assert await get_remaining_calls(_redis(get=b"9999"), "p", "u", "suggester") == 0


@pytest.mark.asyncio
async def test_remaining_calls_redis_failure_returns_full_limit(caplog):
    import logging

    from ontokit.services.llm.rate_limiter import FAIL_OPEN_EVENT

    redis = AsyncMock()
    redis.get = AsyncMock(side_effect=ConnectionError("redis down"))
    with caplog.at_level(logging.WARNING, logger="ontokit.services.llm.rate_limiter"):
        assert await get_remaining_calls(redis, "p", "u", "suggester") == 100

    # The remaining-calls path fails open too and must emit the same alert marker.
    records = [r for r in caplog.records if getattr(r, "event", None) == FAIL_OPEN_EVENT]
    assert len(records) == 1
    assert records[0].operation == "get_remaining_calls"
