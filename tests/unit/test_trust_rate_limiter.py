"""Tests for the untrusted-rung submission limiter (U6, R10, R17).

The important assertion here is the direction of failure. The LLM rate limiter
fails OPEN because it meters cost; this one fails CLOSED because it is an abuse
control, and degrading it open reopens exactly the hole R10 exists to close.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from redis.exceptions import RedisError

from ontokit.services.trust_rate_limiter import (
    UNAVAILABLE_EVENT,
    check_and_consume,
    get_remaining,
    submission_key,
)

PROJECT = "proj-1"
USER = "user-1"


def _redis(*, count: int = 1, get_value: bytes | None = None) -> AsyncMock:
    redis = AsyncMock()
    redis.incr = AsyncMock(return_value=count)
    redis.expire = AsyncMock(return_value=True)
    redis.get = AsyncMock(return_value=get_value)
    return redis


class TestSubmissionKey:
    def test_key_is_scoped_by_project_user_and_utc_day(self) -> None:
        key = submission_key(PROJECT, USER, "2026-07-24")
        assert key == "trust:submit:proj-1:user-1:2026-07-24"

    def test_day_defaults_to_today(self) -> None:
        assert submission_key(PROJECT, USER).startswith("trust:submit:proj-1:user-1:20")

    def test_different_days_are_different_budgets(self) -> None:
        assert submission_key(PROJECT, USER, "2026-07-24") != submission_key(
            PROJECT, USER, "2026-07-25"
        )


class TestCheckAndConsume:
    async def test_first_submission_is_allowed_and_sets_a_ttl(self) -> None:
        redis = _redis(count=1)
        allowed, remaining = await check_and_consume(redis, PROJECT, USER, limit=10)
        assert allowed is True
        assert remaining == 9
        redis.expire.assert_awaited_once()
        assert redis.expire.await_args.kwargs == {"nx": True}

    async def test_later_submission_repairs_a_missing_ttl_without_extending_one(self) -> None:
        redis = _redis(count=4)
        await check_and_consume(redis, PROJECT, USER, limit=10)
        redis.expire.assert_awaited_once()
        assert redis.expire.await_args.kwargs == {"nx": True}

    async def test_submission_at_the_limit_is_allowed(self) -> None:
        allowed, remaining = await check_and_consume(_redis(count=10), PROJECT, USER, limit=10)
        assert allowed is True
        assert remaining == 0

    async def test_submission_past_the_limit_is_denied(self) -> None:
        allowed, remaining = await check_and_consume(_redis(count=11), PROJECT, USER, limit=10)
        assert allowed is False
        assert remaining == 0

    async def test_zero_limit_denies_everything(self) -> None:
        redis = _redis()
        allowed, _ = await check_and_consume(redis, PROJECT, USER, limit=0)
        assert allowed is False
        redis.incr.assert_not_awaited()

    async def test_limit_defaults_to_the_setting(self) -> None:
        with patch("ontokit.services.trust_rate_limiter.settings") as mock_settings:
            mock_settings.untrusted_daily_suggestion_limit = 3
            allowed, remaining = await check_and_consume(_redis(count=3), PROJECT, USER)
        assert allowed is True
        assert remaining == 0

    async def test_missing_client_fails_closed(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("WARNING", logger="ontokit.services.trust_rate_limiter"):
            allowed, _ = await check_and_consume(None, PROJECT, USER, limit=10)
        assert allowed is False
        assert any(UNAVAILABLE_EVENT in r.message for r in caplog.records)

    @pytest.mark.parametrize(
        "error", [RedisError("down"), ConnectionError("refused"), TimeoutError(), OSError()]
    )
    async def test_infrastructure_failure_fails_closed(
        self, error: BaseException, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Deliberately opposite to the LLM limiter's fail-open posture."""
        redis = _redis()
        redis.incr = AsyncMock(side_effect=error)
        with caplog.at_level("WARNING", logger="ontokit.services.trust_rate_limiter"):
            allowed, remaining = await check_and_consume(redis, PROJECT, USER, limit=10)
        assert allowed is False
        assert remaining == 0
        assert any(UNAVAILABLE_EVENT in r.message for r in caplog.records)

    async def test_programming_errors_are_not_swallowed(self) -> None:
        """A mis-wired client must surface, not masquerade as 'Redis down'."""
        redis = _redis()
        redis.incr = AsyncMock(side_effect=TypeError("bad wiring"))
        with pytest.raises(TypeError):
            await check_and_consume(redis, PROJECT, USER, limit=10)


class TestGetRemaining:
    async def test_reads_without_consuming(self) -> None:
        redis = _redis(get_value=b"4")
        assert await get_remaining(redis, PROJECT, USER, limit=10) == 6
        redis.incr.assert_not_awaited()

    async def test_no_key_means_the_full_budget(self) -> None:
        assert await get_remaining(_redis(get_value=None), PROJECT, USER, limit=10) == 10

    async def test_never_reports_a_negative_budget(self) -> None:
        assert await get_remaining(_redis(get_value=b"25"), PROJECT, USER, limit=10) == 0

    async def test_missing_client_reports_zero(self) -> None:
        assert await get_remaining(None, PROJECT, USER, limit=10) == 0

    async def test_redis_error_reports_zero(self) -> None:
        redis = _redis()
        redis.get = AsyncMock(side_effect=RedisError("down"))
        assert await get_remaining(redis, PROJECT, USER, limit=10) == 0
