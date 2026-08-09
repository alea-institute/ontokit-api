"""Process-local serialization for branch read-modify-write operations."""

from __future__ import annotations

import asyncio
from uuid import UUID
from weakref import WeakValueDictionary

_branch_locks: WeakValueDictionary[tuple[UUID, str], asyncio.Lock] = WeakValueDictionary()


def branch_write_lock(project_id: UUID, branch: str) -> asyncio.Lock:
    """Return the shared in-process lock for one project branch."""
    key = (project_id, branch)
    lock = _branch_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _branch_locks[key] = lock
    return lock


__all__ = ["branch_write_lock"]
