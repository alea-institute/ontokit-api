"""Real-PostgreSQL proof that the change-history migrations apply, revert, and reapply.

Each test works in a dedicated, throwaway database and drops it afterwards. The
tests skip when ``DATABASE_URL`` is unset or the role cannot create databases.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.integration

ROOT = Path(__file__).parents[2]
_DATABASE_URL = os.environ.get("DATABASE_URL")
BEFORE = "i7j8k9l0m1n2"
ORIGIN = "j8k9l0m1n2o3"
WATERMARKS = "k9l0m1n2o3p4"
ORIGIN_INDEX = "ix_change_events_project_origin_time"
COMMIT_INDEX = "ix_change_events_project_branch_commit"
SEEDED_EVENTS = 2500


def _url(database: str, *, driver: str) -> str:
    assert _DATABASE_URL is not None
    url = make_url(_DATABASE_URL).set(drivername=driver, database=database)
    return url.render_as_string(hide_password=False)


def _alembic(database: str, *arguments: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": _url(database, driver="postgresql+asyncpg")},
        capture_output=True,
        text=True,
        timeout=240,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest_asyncio.fixture
async def database() -> AsyncIterator[asyncpg.Connection]:
    """Connect to an empty dedicated database, dropped afterwards."""
    if not _DATABASE_URL:
        pytest.skip("DATABASE_URL not set")

    name = f"ontokit_change_history_{uuid4().hex[:12]}"
    admin = await asyncpg.connect(
        _url(make_url(_DATABASE_URL).database or "postgres", driver="postgresql")
    )
    try:
        try:
            await admin.execute(f'CREATE DATABASE "{name}"')
        except asyncpg.PostgresError as exc:  # pragma: no cover - privilege dependent
            pytest.skip(f"cannot create a scratch database: {exc}")
        connection = await asyncpg.connect(_url(name, driver="postgresql"))
        try:
            yield connection
        finally:
            await connection.close()
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await admin.close()


async def _name(connection: asyncpg.Connection) -> str:
    return await connection.fetchval("SELECT current_database()")


async def _seed_events(connection: asyncpg.Connection) -> None:
    """Insert history as the application wrote it before these migrations."""
    project_id = uuid4()
    await connection.execute(
        "INSERT INTO projects (id, name, owner_id, is_public) "
        "VALUES ($1, 'Seeded ontology', 'owner', false)",
        project_id,
    )
    await connection.executemany(
        "INSERT INTO entity_change_events "
        "(id, project_id, branch, entity_iri, entity_type, event_type, user_id, commit_hash, "
        "changed_fields) VALUES ($1, $2, 'main', $3, 'class', 'update', 'owner', $4, '[]')",
        [
            (uuid4(), project_id, f"https://example.org/ontology#Class{number}", f"{number:040x}")
            for number in range(SEEDED_EVENTS)
        ],
    )


async def _schema(connection: asyncpg.Connection) -> dict[str, object]:
    return {
        "revision": await connection.fetchval("SELECT version_num FROM alembic_version"),
        "origin_columns": sorted(
            row["column_name"]
            for row in await connection.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'entity_change_events' AND column_name LIKE 'origin%'"
            )
        ),
        "tables": sorted(
            row["tablename"]
            for row in await connection.fetch(
                "SELECT tablename FROM pg_tables WHERE tablename IN "
                "('llm_generated_entities', 'change_history_watermarks')"
            )
        ),
        # A concurrent build that failed leaves an index behind marked invalid.
        "valid_indexes": sorted(
            row["relname"]
            for row in await connection.fetch(
                "SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname = ANY($1) AND i.indisvalid AND i.indisready AND NOT i.indisunique",
                [ORIGIN_INDEX, COMMIT_INDEX],
            )
        ),
    }


UPGRADED = {
    "revision": WATERMARKS,
    "origin_columns": ["origin", "origin_batch_id", "origin_model"],
    "tables": ["change_history_watermarks", "llm_generated_entities"],
    "valid_indexes": [COMMIT_INDEX, ORIGIN_INDEX],
}
REVERTED = {"revision": BEFORE, "origin_columns": [], "tables": [], "valid_indexes": []}


@pytest.mark.asyncio
@pytest.mark.parametrize("seeded", [False, True])
async def test_upgrade_downgrade_and_upgrade_again(
    database: asyncpg.Connection, seeded: bool
) -> None:
    name = await _name(database)
    _alembic(name, "upgrade", BEFORE)
    expected_events = 0
    if seeded:
        await _seed_events(database)
        expected_events = SEEDED_EVENTS

    # CREATE INDEX CONCURRENTLY refuses to run inside a transaction block, so a
    # successful upgrade shows both indexes were built outside one.
    _alembic(name, "upgrade", WATERMARKS)
    assert await _schema(database) == UPGRADED
    assert await database.fetchval(
        "SELECT count(*) FROM entity_change_events WHERE origin = 'human'"
    ) == (expected_events)

    if seeded:
        project_id = await database.fetchval("SELECT id FROM projects")
        await database.execute(
            "UPDATE entity_change_events SET origin = 'ai', origin_model = 'example-model', "
            "origin_batch_id = $1 WHERE entity_iri LIKE '%Class1'",
            uuid4(),
        )
        await database.execute(
            "INSERT INTO change_history_watermarks (project_id, branch, commit_hash) "
            "VALUES ($1, 'main', $2)",
            project_id,
            "a" * 40,
        )
        await database.execute(
            "INSERT INTO llm_generated_entities (id, project_id, branch, entity_iri, batch_id, "
            "suggestion_type, focus_iri, label, user_id) "
            "VALUES ($1, $2, 'main', 'https://example.org/ontology#Class1', $3, 'children', "
            "'https://example.org/ontology#Focus', 'Class 1', 'owner')",
            uuid4(),
            project_id,
            uuid4(),
        )

    _alembic(name, "downgrade", BEFORE)
    assert await _schema(database) == REVERTED
    assert await database.fetchval("SELECT count(*) FROM entity_change_events") == expected_events

    _alembic(name, "upgrade", WATERMARKS)
    assert await _schema(database) == UPGRADED
    assert await database.fetchval(
        "SELECT count(*) FROM entity_change_events WHERE origin = 'human'"
    ) == (expected_events)


@pytest.mark.asyncio
@pytest.mark.parametrize("revision", [ORIGIN, WATERMARKS])
async def test_upgrade_resumes_after_an_interrupted_concurrent_index_build(
    database: asyncpg.Connection, revision: str
) -> None:
    """A concurrent build commits the steps before it, so a rerun must tolerate them."""
    name = await _name(database)
    previous = BEFORE if revision == ORIGIN else ORIGIN
    _alembic(name, "upgrade", previous)
    await _seed_events(database)

    # Reproduce the state a killed build leaves: earlier steps committed, the
    # index present but invalid, and the revision not yet recorded.
    if revision == ORIGIN:
        await database.execute(
            "ALTER TABLE entity_change_events "
            "ADD COLUMN origin VARCHAR(20) NOT NULL DEFAULT 'human', "
            "ADD COLUMN origin_model VARCHAR(255), ADD COLUMN origin_batch_id UUID"
        )
        index, columns = ORIGIN_INDEX, "branch"
    else:
        await database.execute(
            "CREATE TABLE change_history_watermarks ("
            "project_id UUID REFERENCES projects (id) ON DELETE CASCADE, branch VARCHAR(255), "
            "commit_hash VARCHAR(40) NOT NULL, "
            "updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (project_id, branch))"
        )
        index, columns = COMMIT_INDEX, "branch"
    with pytest.raises(asyncpg.UniqueViolationError):
        await database.execute(
            f"CREATE UNIQUE INDEX CONCURRENTLY {index} ON entity_change_events ({columns})"
        )
    assert not await database.fetchval(
        "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "WHERE c.relname = $1",
        index,
    )

    _alembic(name, "upgrade", WATERMARKS)

    assert await _schema(database) == UPGRADED
    assert (
        await database.fetchval("SELECT count(*) FROM entity_change_events WHERE origin = 'human'")
        == SEEDED_EVENTS
    )


@pytest.mark.asyncio
async def test_downgrade_resumes_after_its_concurrent_index_drop(
    database: asyncpg.Connection,
) -> None:
    name = await _name(database)
    _alembic(name, "upgrade", WATERMARKS)
    for index in (ORIGIN_INDEX, COMMIT_INDEX):
        await database.execute(f"DROP INDEX CONCURRENTLY {index}")

    _alembic(name, "downgrade", BEFORE)

    assert await _schema(database) == REVERTED


@pytest.mark.asyncio
async def test_upgrade_gives_up_instead_of_queueing_behind_a_long_reader(
    database: asyncpg.Connection,
) -> None:
    """A waiting ALTER would stall every later query on the table; it must time out."""
    name = await _name(database)
    _alembic(name, "upgrade", BEFORE)
    await _seed_events(database)
    reader = await asyncpg.connect(_url(name, driver="postgresql"))
    try:
        reading = reader.transaction()
        await reading.start()
        await reader.fetchval("SELECT count(*) FROM entity_change_events")

        blocked = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", ORIGIN],
            cwd=ROOT,
            env={**os.environ, "DATABASE_URL": _url(name, driver="postgresql+asyncpg")},
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert blocked.returncode != 0
        assert "lock timeout" in blocked.stderr
        assert await _schema(database) == REVERTED
        await reading.rollback()
    finally:
        await reader.close()

    # Nothing was committed, so the rerun applies the whole revision.
    _alembic(name, "upgrade", WATERMARKS)
    assert await _schema(database) == UPGRADED


TRANSACTIONS = "77a67de3294e"


async def _recorded_xact(connection: asyncpg.Connection) -> tuple[str | None, str | None]:
    """Report the revision and the recorded_xact column's default, if the column exists."""
    revision = await connection.fetchval("SELECT version_num FROM alembic_version")
    default = await connection.fetchval(
        "SELECT coalesce(column_default, '') FROM information_schema.columns "
        "WHERE table_name = 'entity_change_events' AND column_name = 'recorded_xact'"
    )
    return revision, default


async def _run_behind_a_reader(database: asyncpg.Connection, *arguments: str) -> None:
    """Run alembic while another session reads the table; it must give up, not wait."""
    reader = await asyncpg.connect(_url(await _name(database), driver="postgresql"))
    try:
        reading = reader.transaction()
        await reading.start()
        await reader.fetchval("SELECT count(*) FROM entity_change_events")

        blocked = subprocess.run(
            [sys.executable, "-m", "alembic", *arguments],
            cwd=ROOT,
            env={
                **os.environ,
                "DATABASE_URL": _url(await _name(database), driver="postgresql+asyncpg"),
            },
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert blocked.returncode != 0
        assert "lock timeout" in blocked.stderr
        await reading.rollback()
    finally:
        await reader.close()


@pytest.mark.asyncio
async def test_transaction_column_gives_up_instead_of_queueing_behind_a_long_reader(
    database: asyncpg.Connection,
) -> None:
    """Both directions of the recorded_xact revision time out, commit nothing, and rerun."""
    name = await _name(database)
    _alembic(name, "upgrade", WATERMARKS)
    await _seed_events(database)

    await _run_behind_a_reader(database, "upgrade", TRANSACTIONS)
    assert await _recorded_xact(database) == (WATERMARKS, None)

    _alembic(name, "upgrade", TRANSACTIONS)
    revision, default = await _recorded_xact(database)
    assert revision == TRANSACTIONS
    assert default is not None and "pg_current_xact_id" in default

    await _run_behind_a_reader(database, "downgrade", WATERMARKS)
    assert await _recorded_xact(database) == (TRANSACTIONS, default)

    _alembic(name, "downgrade", WATERMARKS)
    assert await _recorded_xact(database) == (WATERMARKS, None)
