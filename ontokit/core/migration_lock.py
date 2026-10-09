"""Cross-process advisory lock that serializes concurrent ``alembic upgrade`` runs.

Used by ``alembic/env.py``. Kept in an importable module (``env.py`` runs
migrations at import) so the lock behaviour can be unit tested without a
database.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable

from sqlalchemy import text
from sqlalchemy.engine import Connection

logger = logging.getLogger("alembic.env")

# Session-level advisory lock key that serializes concurrent ``alembic upgrade``
# runners (e.g. api and worker containers starting from the same image). Fixed
# signed 64-bit value derived from "ontokit:alembic:migrations"; keep it stable.
MIGRATION_ADVISORY_LOCK_KEY = 0x6F6E746F6B697401
MIGRATION_LOCK_POLL_SECONDS = 0.5
MIGRATION_LOCK_TIMEOUT_SECONDS = float(os.environ.get("ONTOKIT_MIGRATION_LOCK_TIMEOUT", "900"))


def _autocommit_scalar(connection: Connection, sql: str) -> object:
    """Run one statement outside any transaction and return its scalar."""
    if connection.in_transaction():
        connection.rollback()
    default_isolation = connection.default_isolation_level
    connection.execution_options(isolation_level="AUTOCOMMIT")
    try:
        return connection.execute(text(sql), {"key": MIGRATION_ADVISORY_LOCK_KEY}).scalar()
    finally:
        # Under AUTOCOMMIT the DBAPI commits each statement, but SQLAlchemy still
        # tracks a logical transaction that must end before isolation changes.
        if connection.in_transaction():
            connection.rollback()
        connection.execution_options(isolation_level=default_isolation)


def acquire_migration_lock(
    connection: Connection,
    *,
    timeout_seconds: float | None = None,
    poll_seconds: float | None = None,
) -> None:
    """Take the session-level migration lock, polling without a transaction.

    A blocking ``pg_advisory_lock`` would deadlock: the waiting statement keeps
    a virtual transaction open, and the leader's ``CREATE INDEX CONCURRENTLY``
    waits for every open transaction. Short ``pg_try_advisory_lock`` attempts in
    autocommit mode never hold a transaction across the wait.

    Raises:
        TimeoutError: the lock was not obtained within ``timeout_seconds``.
    """
    timeout = MIGRATION_LOCK_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    poll = MIGRATION_LOCK_POLL_SECONDS if poll_seconds is None else poll_seconds
    deadline = time.monotonic() + timeout
    waiting_logged = False
    while not _autocommit_scalar(connection, "SELECT pg_try_advisory_lock(:key)"):
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Timed out waiting for the alembic migration advisory lock after {timeout:.0f}s"
            )
        if not waiting_logged:
            logger.info("Another runner holds the migration lock; waiting for it")
            waiting_logged = True
        time.sleep(poll)


def release_migration_lock(connection: Connection) -> None:
    """Release the session-level migration lock taken by ``acquire_migration_lock``."""
    _autocommit_scalar(connection, "SELECT pg_advisory_unlock(:key)")


def run_with_migration_lock(connection: Connection, run: Callable[[], None]) -> None:
    """Run ``run`` while holding the migration lock on PostgreSQL.

    The lock is held on this same connection for the whole run, so a second
    concurrent runner waits and then sees the schema already at head instead of
    colliding on DDL. Other dialects (e.g. SQLite in tests) skip the lock. The
    unlock always runs, even when ``run`` raises.
    """
    use_lock = connection.dialect.name == "postgresql"
    if use_lock:
        acquire_migration_lock(connection)
    try:
        run()
    finally:
        if use_lock:
            release_migration_lock(connection)
