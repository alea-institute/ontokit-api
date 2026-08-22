"""Cross-process serialization for branch read-modify-write operations."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from uuid import UUID
from weakref import WeakValueDictionary

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_branch_locks: WeakValueDictionary[tuple[UUID, str], asyncio.Lock] = WeakValueDictionary()
_embedding_config_locks: WeakValueDictionary[UUID, asyncio.Lock] = WeakValueDictionary()


async def _acquire_transaction_lock(db: AsyncSession, lock_key: str) -> None:
    """Acquire one collision-resistant advisory lock for the active transaction."""
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": lock_key},
    )


@asynccontextmanager
async def branch_write_lock(db: AsyncSession, project_id: UUID, branch: str) -> AsyncIterator[None]:
    """Exclude concurrent writers to a branch in this process and every DB-sharing process.

    The process-local asyncio lock is paired with a transaction-scoped PostgreSQL advisory
    lock using the canonical ``branch:{project}:{branch}`` key.  The guarantee lasts until
    this context exits locally and until the active database transaction ends cross-process;
    callers must commit or roll back that transaction before permitting dependent later work.
    """
    key = (project_id, branch)
    lock = _branch_locks.setdefault(key, asyncio.Lock())
    async with lock:
        await _acquire_transaction_lock(db, f"branch:{project_id}:{branch}")
        yield


@asynccontextmanager
async def embedding_config_lock(db: AsyncSession, project_id: UUID) -> AsyncIterator[None]:
    """Serialize embedding configuration changes with snapshot activation."""
    lock = _embedding_config_locks.setdefault(project_id, asyncio.Lock())
    async with lock:
        await _acquire_transaction_lock(db, f"embedding-config:{project_id}")
        yield


@asynccontextmanager
async def branch_write_locks(
    db: AsyncSession, project_id: UUID, branches: set[str]
) -> AsyncIterator[None]:
    """Acquire multiple canonical branch locks in deadlock-safe order."""
    async with AsyncExitStack() as stack:
        for branch in sorted(branches):
            await stack.enter_async_context(branch_write_lock(db, project_id, branch))
        yield


__all__ = ["branch_write_lock", "branch_write_locks", "embedding_config_lock"]
