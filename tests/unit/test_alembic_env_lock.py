"""Unit tests for the alembic migration advisory lock (no database needed).

``alembic/env.py`` delegates to ``ontokit.core.migration_lock``; the real
PostgreSQL proof lives in ``tests/integration/test_migration_advisory_lock.py``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ontokit.core import migration_lock

ROOT = Path(__file__).parents[2]


def _connection(dialect: str, try_lock_results: list[bool] | None = None) -> MagicMock:
    """A mock sync Connection recording every SQL statement it executes."""
    conn = MagicMock()
    conn.dialect.name = dialect
    conn.in_transaction.return_value = False
    conn.default_isolation_level = "READ COMMITTED"
    conn.executed = []
    results = iter(try_lock_results or [True])

    def execute(clause: object, params: object = None) -> MagicMock:  # noqa: ARG001
        sql = str(clause)
        conn.executed.append(sql)
        result = MagicMock()
        if "pg_try_advisory_lock" in sql:
            result.scalar.return_value = next(results)
        else:
            result.scalar.return_value = True
        return result

    conn.execute.side_effect = execute
    return conn


def _statements(conn: MagicMock, needle: str) -> list[str]:
    return [sql for sql in conn.executed if needle in sql]


def test_lock_timeout_raises_timeout_error(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _connection("postgresql", try_lock_results=[False] * 1000)
    sleeps: list[float] = []
    monkeypatch.setattr(migration_lock.time, "sleep", sleeps.append)

    with pytest.raises(TimeoutError, match="migration advisory lock"):
        migration_lock.acquire_migration_lock(conn, timeout_seconds=0.0, poll_seconds=0.01)

    assert _statements(conn, "pg_try_advisory_lock")


def test_lock_polls_until_available(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _connection("postgresql", try_lock_results=[False, False, True])
    sleeps: list[float] = []
    monkeypatch.setattr(migration_lock.time, "sleep", sleeps.append)

    migration_lock.acquire_migration_lock(conn, timeout_seconds=60, poll_seconds=0.01)

    assert len(_statements(conn, "pg_try_advisory_lock")) == 3
    assert sleeps == [0.01, 0.01]


def test_lock_attempts_run_in_autocommit() -> None:
    conn = _connection("postgresql")
    migration_lock.acquire_migration_lock(conn, timeout_seconds=1)

    isolation_calls = [
        c.kwargs.get("isolation_level") for c in conn.execution_options.call_args_list
    ]
    assert isolation_calls == ["AUTOCOMMIT", "READ COMMITTED"]


def test_non_postgresql_dialect_skips_the_lock() -> None:
    conn = _connection("sqlite")
    ran: list[bool] = []

    migration_lock.run_with_migration_lock(conn, lambda: ran.append(True))

    assert ran == [True]
    assert conn.executed == []


def test_postgresql_takes_and_releases_the_lock_around_the_run() -> None:
    conn = _connection("postgresql")
    order: list[str] = []

    def run() -> None:
        order.append(f"run after {len(conn.executed)} statements")

    migration_lock.run_with_migration_lock(conn, run)

    assert order == ["run after 1 statements"]
    assert "pg_try_advisory_lock" in conn.executed[0]
    assert "pg_advisory_unlock" in conn.executed[-1]


def test_unlock_runs_in_finally_after_failing_migration() -> None:
    conn = _connection("postgresql")

    def failing_run() -> None:
        raise RuntimeError("migration failed")

    with pytest.raises(RuntimeError, match="migration failed"):
        migration_lock.run_with_migration_lock(conn, failing_run)

    assert len(_statements(conn, "pg_advisory_unlock")) == 1


def test_unlock_rolls_back_an_open_failed_transaction_first() -> None:
    conn = _connection("postgresql")
    conn.in_transaction.return_value = True

    migration_lock.release_migration_lock(conn)

    assert conn.rollback.called
    assert _statements(conn, "pg_advisory_unlock")


def test_alembic_env_delegates_to_the_lock_helper() -> None:
    source = (ROOT / "alembic" / "env.py").read_text(encoding="utf-8")
    assert "from ontokit.core.migration_lock import run_with_migration_lock" in source
    assert "run_with_migration_lock(connection," in source
