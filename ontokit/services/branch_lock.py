"""Cross-process serialization for branch read-modify-write operations."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID
from weakref import WeakValueDictionary

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_branch_locks: WeakValueDictionary[tuple[UUID, str], asyncio.Lock] = WeakValueDictionary()


@asynccontextmanager
async def branch_write_lock(
    db: AsyncSession, project_id: UUID, branch: str
) -> AsyncIterator[None]:
    """Exclude concurrent writers to a branch in this process and every DB-sharing process.

    The process-local asyncio lock is paired with a transaction-scoped PostgreSQL advisory
    lock using the canonical ``suggestion:{project}:{branch}`` key.  The guarantee lasts until
    this context exits locally and until the active database transaction ends cross-process;
    callers must commit or roll back that transaction before permitting dependent later work.
    """
    key = (project_id, branch)
    lock = _branch_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _branch_locks[key] = lock
    async with lock:
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
            {"lock_key": f"suggestion:{project_id}:{branch}"},
        )
        yield


__all__ = ["branch_write_lock"]
