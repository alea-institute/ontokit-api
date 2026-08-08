"""Integration test fixtures with real database and Redis."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://ontokit:ontokit_test@127.0.0.1:5433/ontokit_test",
)
_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:6380/0")

needs_db = pytest.mark.integration
needs_redis = pytest.mark.integration


@pytest.fixture(scope="session", autouse=True)
def migrated_test_database() -> None:
    """Apply every migration to the dedicated integration database once per run."""
    env = os.environ.copy()
    env["DATABASE_URL"] = _DATABASE_URL
    env["REDIS_URL"] = _REDIS_URL
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=True,
        cwd=os.getcwd(),
        env=env,
    )


@pytest_asyncio.fixture
async def real_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Create a real async database session, rolling back after each test."""
    engine = create_async_engine(_DATABASE_URL, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        yield session
        await session.rollback()

    await engine.dispose()


@pytest_asyncio.fixture
async def real_redis() -> AsyncGenerator[object, None]:
    """Create a real Redis client."""
    import redis.asyncio as aioredis

    client = aioredis.from_url(_REDIS_URL)  # type: ignore[no-untyped-call]
    yield client
    await client.aclose()
