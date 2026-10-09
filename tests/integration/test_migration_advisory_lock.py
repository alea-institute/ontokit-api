"""Real-PostgreSQL proof that concurrent ``alembic upgrade head`` runs serialize.

Each test creates a dedicated, throwaway database next to the shared test
database (never touching its schema) and drops it afterwards. The tests skip
when ``DATABASE_URL`` is unset or the role cannot create databases.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy.engine import make_url

from ontokit.core.migration_lock import MIGRATION_ADVISORY_LOCK_KEY

pytestmark = pytest.mark.integration

ROOT = Path(__file__).parents[2]
_DATABASE_URL = os.environ.get("DATABASE_URL")
_SUBPROCESS_TIMEOUT = 240


def _load_lock_key() -> int:
    """Return the lock key ``alembic/env.py`` takes (via ``ontokit.core.migration_lock``)."""
    env_source = (ROOT / "alembic" / "env.py").read_text(encoding="utf-8")
    assert "run_with_migration_lock" in env_source, "alembic/env.py must take the lock"
    return MIGRATION_ADVISORY_LOCK_KEY


def _asyncpg_dsn(database: str) -> str:
    assert _DATABASE_URL is not None
    url = make_url(_DATABASE_URL).set(drivername="postgresql", database=database)
    return url.render_as_string(hide_password=False)


def _sqlalchemy_url(database: str) -> str:
    assert _DATABASE_URL is not None
    url = make_url(_DATABASE_URL).set(database=database)
    return url.render_as_string(hide_password=False)


@pytest_asyncio.fixture
async def scratch_database() -> AsyncIterator[str]:
    """Yield the name of an empty dedicated database, dropped afterwards."""
    if not _DATABASE_URL:
        pytest.skip("DATABASE_URL not set")

    name = f"ontokit_alembic_lock_{uuid4().hex[:12]}"
    admin_db = make_url(_DATABASE_URL).database or "postgres"
    admin = await asyncpg.connect(_asyncpg_dsn(admin_db))
    try:
        try:
            await admin.execute(f'CREATE DATABASE "{name}"')
        except asyncpg.PostgresError as exc:  # pragma: no cover - privilege dependent
            pytest.skip(f"cannot create a scratch database: {exc}")
        try:
            yield name
        finally:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await admin.close()


def _start_upgrade(database: str) -> subprocess.Popen[str]:
    env = {**os.environ, "DATABASE_URL": _sqlalchemy_url(database)}
    return subprocess.Popen(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _head_revision() -> str:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    heads = script.get_heads()
    assert len(heads) == 1, heads
    return heads[0]


async def _current_revision(database: str) -> str | None:
    conn = await asyncpg.connect(_asyncpg_dsn(database))
    try:
        exists = await conn.fetchval("SELECT to_regclass('public.alembic_version') IS NOT NULL")
        if not exists:
            return None
        return await conn.fetchval("SELECT version_num FROM alembic_version")
    finally:
        await conn.close()


async def _finish(runner: subprocess.Popen[str]) -> str:
    output, _ = await asyncio.to_thread(runner.communicate, timeout=_SUBPROCESS_TIMEOUT)
    return output


@pytest.mark.asyncio
async def test_concurrent_upgrades_on_empty_database_both_succeed(
    scratch_database: str,
) -> None:
    """AE3: two simultaneous ``alembic upgrade head`` runs both exit 0."""
    runners = [_start_upgrade(scratch_database) for _ in range(2)]
    outputs = await asyncio.gather(*(_finish(runner) for runner in runners))

    for runner, output in zip(runners, outputs, strict=True):
        assert runner.returncode == 0, output
    assert await _current_revision(scratch_database) == _head_revision()


@pytest.mark.asyncio
async def test_upgrade_waits_for_the_migration_advisory_lock(
    scratch_database: str,
) -> None:
    """A runner blocks while another session holds the lock, then completes.

    Deterministic proof that ``alembic/env.py`` takes the advisory lock: without
    it, the runner would migrate the empty database while the lock is held.
    """
    key = _load_lock_key()
    holder = await asyncpg.connect(_asyncpg_dsn(scratch_database))
    try:
        assert await holder.fetchval("SELECT pg_try_advisory_lock($1)", key) is True
        runner = _start_upgrade(scratch_database)
        try:
            # Give the runner ample time to connect; it must still be waiting.
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                assert runner.poll() is None, runner.communicate()[0]
                await asyncio.sleep(0.25)
            assert await _current_revision(scratch_database) is None
        finally:
            assert await holder.fetchval("SELECT pg_advisory_unlock($1)", key) is True
        output = await _finish(runner)
    finally:
        await holder.close()

    assert runner.returncode == 0, output
    assert await _current_revision(scratch_database) == _head_revision()
