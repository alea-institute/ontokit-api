"""Alembic environment configuration for async SQLAlchemy."""

import asyncio
import logging
import os
import time
from logging.config import fileConfig

from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

logger = logging.getLogger("alembic.env")

# Import all models so they are registered with Base.metadata
from ontokit.core.database import Base  # noqa: E402
from ontokit.models import (  # noqa: E402, F401
    BranchMetadata,
    IndexedAnnotation,
    IndexedEntity,
    IndexedHierarchy,
    IndexedLabel,
    JoinRequest,
    NormalizationRun,
    OntologyIndexStatus,
    Project,
    ProjectMember,
    UserGitHubToken,
)

target_metadata = Base.metadata

# Get database URL from app settings
from ontokit.core.config import settings  # noqa: E402

# Convert asyncpg URL to use psycopg2 for sync operations if needed
database_url = str(settings.database_url)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


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


def _acquire_migration_lock(connection: Connection) -> None:
    """Take the session-level migration lock, polling without a transaction.

    A blocking ``pg_advisory_lock`` would deadlock: the waiting statement keeps
    a virtual transaction open, and the leader's ``CREATE INDEX CONCURRENTLY``
    waits for every open transaction. Short ``pg_try_advisory_lock`` attempts in
    autocommit mode never hold a transaction across the wait.
    """
    deadline = time.monotonic() + MIGRATION_LOCK_TIMEOUT_SECONDS
    waiting_logged = False
    while not _autocommit_scalar(connection, "SELECT pg_try_advisory_lock(:key)"):
        if time.monotonic() >= deadline:
            raise TimeoutError(
                "Timed out waiting for the alembic migration advisory lock "
                f"after {MIGRATION_LOCK_TIMEOUT_SECONDS:.0f}s"
            )
        if not waiting_logged:
            logger.info("Another runner holds the migration lock; waiting for it")
            waiting_logged = True
        time.sleep(MIGRATION_LOCK_POLL_SECONDS)


def do_run_migrations(connection: Connection) -> None:
    """Run migrations with the given connection.

    On PostgreSQL, a session-level advisory lock is held on this same
    connection for the whole run, so a second concurrent runner waits and then
    sees the schema already at head instead of colliding on DDL.
    """
    use_lock = connection.dialect.name == "postgresql"
    if use_lock:
        _acquire_migration_lock(connection)
    try:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    finally:
        if use_lock:
            _autocommit_scalar(connection, "SELECT pg_advisory_unlock(:key)")


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode with async engine."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = database_url

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
