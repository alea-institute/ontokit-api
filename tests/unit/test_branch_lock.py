"""Concurrency contracts for the canonical branch write lock."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from ontokit.services.branch_lock import branch_write_lock

PROJECT_ID = UUID("12345678-1234-5678-1234-567812345678")


@pytest.mark.asyncio
async def test_same_branch_writers_share_one_lock_identity_and_serialize() -> None:
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()
    execute_count = 0

    async def execute(*_args: object, **_kwargs: object) -> object:
        nonlocal execute_count
        execute_count += 1
        if execute_count == 1:
            first_entered.set()
            await release_first.wait()
        else:
            second_entered.set()
        return object()

    db = AsyncMock()
    db.execute.side_effect = execute

    async def writer() -> None:
        async with branch_write_lock(db, PROJECT_ID, "main"):
            await asyncio.sleep(0)

    first = asyncio.create_task(writer())
    await first_entered.wait()
    second = asyncio.create_task(writer())
    await asyncio.sleep(0)

    assert not second_entered.is_set()
    release_first.set()
    await asyncio.gather(first, second)

    assert db.execute.await_count == 2
    for call in db.execute.await_args_list:
        assert call.args[1] == {"lock_key": f"branch:{PROJECT_ID}:main"}
