"""Configurable per-IP anonymous session limit (BAIF demo, U5).

A conference room of ~30 attendees shares one NAT address, so the demo must
be able to raise the per-IP hourly anonymous-session limit without code.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from ontokit.core.config import Settings, settings
from ontokit.services.suggestion_service import SuggestionService


@pytest.fixture(autouse=True)
def _secure_test_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Session creation mints tokens; keep tests off the insecure default key."""
    test_settings = type("Settings", (), {"secret_key": "test-secret-key-long-enough"})()
    monkeypatch.setattr("ontokit.core.anonymous_token.settings", test_settings)
    monkeypatch.setattr("ontokit.core.beacon_token.settings", test_settings)


def _service_with_existing(count: int) -> SuggestionService:
    service = SuggestionService.__new__(SuggestionService)
    project = MagicMock()
    project.is_public = True
    service._get_project = AsyncMock(return_value=project)  # type: ignore[method-assign]

    count_result = MagicMock()
    count_result.scalar.return_value = count
    db = MagicMock()
    db.execute = AsyncMock(return_value=count_result)
    db.add = MagicMock()
    db.commit = AsyncMock()

    async def _refresh(row: object) -> None:
        row.created_at = datetime.now(UTC)  # type: ignore[attr-defined]

    db.refresh = AsyncMock(side_effect=_refresh)
    service.db = db
    service.git_service = MagicMock()
    return service


async def _create_sessions_until_refused(attempts: int) -> int:
    """Simulate ``attempts`` sessions from one IP; return how many were created."""
    created = 0
    for _ in range(attempts):
        service = _service_with_existing(created)
        try:
            await service.create_anonymous_session(uuid4(), "198.51.100.7")
        except HTTPException as exc:
            assert exc.status_code == 429
            break
        created += 1
    return created


def test_default_limit_is_unchanged() -> None:
    assert Settings().anonymous_sessions_per_ip_per_hour == 5


def test_limit_must_be_positive() -> None:
    with pytest.raises(ValueError):
        Settings(anonymous_sessions_per_ip_per_hour=0)


@pytest.mark.asyncio
async def test_default_limit_refuses_the_sixth_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "anonymous_sessions_per_ip_per_hour", 5)
    assert await _create_sessions_until_refused(attempts=10) == 5


@pytest.mark.asyncio
async def test_configured_limit_of_60_allows_30_sessions_from_one_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "anonymous_sessions_per_ip_per_hour", 60)
    assert await _create_sessions_until_refused(attempts=30) == 30


@pytest.mark.asyncio
async def test_configured_limit_is_enforced_at_the_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "anonymous_sessions_per_ip_per_hour", 60)
    with pytest.raises(HTTPException) as exc:
        await _service_with_existing(60).create_anonymous_session(uuid4(), "198.51.100.7")
    assert exc.value.status_code == 429


def _count_sql(service: SuggestionService) -> str:
    statement = service.db.execute.await_args_list[0].args[0]  # type: ignore[attr-defined]
    return str(statement.compile(compile_kwargs={"literal_binds": True}))


@pytest.mark.asyncio
async def test_ipv6_sessions_are_counted_per_slash64(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rotating addresses inside one /64 must not escape the per-IP limit."""
    monkeypatch.setattr(settings, "anonymous_sessions_per_ip_per_hour", 5)
    service = _service_with_existing(0)

    await service.create_anonymous_session(uuid4(), "2001:db8::abcd:1")

    sql = _count_sql(service)
    assert "LIKE '2001:0db8:0000:0000:%'" in sql
    stored = service.db.add.call_args.args[0]  # type: ignore[attr-defined]
    # The full address is kept for audit; only the count is bucketed.
    assert stored.client_ip == "2001:0db8:0000:0000:0000:0000:abcd:0001"


@pytest.mark.asyncio
async def test_ipv6_slash64_at_limit_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "anonymous_sessions_per_ip_per_hour", 5)
    with pytest.raises(HTTPException) as exc:
        await _service_with_existing(5).create_anonymous_session(uuid4(), "2001:db8::beef")
    assert exc.value.status_code == 429


@pytest.mark.asyncio
async def test_ipv4_sessions_are_counted_by_exact_address(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "anonymous_sessions_per_ip_per_hour", 5)
    service = _service_with_existing(0)

    await service.create_anonymous_session(uuid4(), "198.51.100.7")

    sql = _count_sql(service)
    assert "suggestion_sessions.client_ip = '198.51.100.7'" in sql
    assert "LIKE" not in sql
