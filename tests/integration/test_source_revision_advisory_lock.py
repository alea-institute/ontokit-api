"""Real-PostgreSQL proof for cross-session source branch serialization."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ontokit.services.branch_lock import branch_write_lock

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_source_branch_lock_excludes_a_second_database_session(
    real_db_session: AsyncSession,
) -> None:
    """The route's advisory lock is visible beyond its process-local mutex.

    The ``real_db_session`` fixture skips with ``DATABASE_URL not set`` when a
    migrated PostgreSQL test database is unavailable.
    """
    bind = real_db_session.bind
    assert bind is not None
    contender_factory = async_sessionmaker(bind, expire_on_commit=False)
    project_id = uuid4()
    branch = "main"
    lock_key = f"suggestion:{project_id}:{branch}"

    async with contender_factory() as contender:
        async with branch_write_lock(real_db_session, project_id, branch):
            contender_acquired = await contender.scalar(
                text("SELECT pg_try_advisory_xact_lock(hashtext(:lock_key))"),
                {"lock_key": lock_key},
            )
            assert contender_acquired is False
            await contender.rollback()
            await real_db_session.commit()

        acquired_after_release = await contender.scalar(
            text("SELECT pg_try_advisory_xact_lock(hashtext(:lock_key))"),
            {"lock_key": lock_key},
        )
        assert acquired_after_release is True
        await contender.rollback()
