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
_pull_request_allocation_locks: WeakValueDictionary[UUID, asyncio.Lock] = WeakValueDictionary()


@asynccontextmanager
async def _pull_request_allocation_lock(db: AsyncSession, project_id: UUID) -> AsyncIterator[None]:
    """Exclude project-wide PR number allocators.

    Project locks must be acquired before branch locks. That canonical order
    lets project-scoped resources (such as PR numbers) coexist with branch
    mutation without introducing cross-branch deadlocks.
    """
    lock = _pull_request_allocation_locks.get(project_id)
    if lock is None:
        lock = asyncio.Lock()
        _pull_request_allocation_locks[project_id] = lock
    async with lock:
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
            {"lock_key": f"project:{project_id}:pull-request-allocation"},
        )
        yield


@asynccontextmanager
async def branch_write_lock(db: AsyncSession, project_id: UUID, branch: str) -> AsyncIterator[None]:
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


@asynccontextmanager
async def branch_write_locks(
    db: AsyncSession, project_id: UUID, branches: set[str]
) -> AsyncIterator[None]:
    """Acquire multiple canonical branch locks in deadlock-safe order."""
    async with AsyncExitStack() as stack:
        for branch in sorted(branches):
            await stack.enter_async_context(branch_write_lock(db, project_id, branch))
        yield


@asynccontextmanager
async def pull_request_write_locks(
    db: AsyncSession, project_id: UUID, branches: set[str]
) -> AsyncIterator[None]:
    """Serialize project PR-number allocation and affected branch mutation."""
    async with (
        _pull_request_allocation_lock(db, project_id),
        branch_write_locks(db, project_id, branches),
    ):
        yield


__all__ = [
    "branch_write_lock",
    "branch_write_locks",
    "pull_request_write_locks",
]
