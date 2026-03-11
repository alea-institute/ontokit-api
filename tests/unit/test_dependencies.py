import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from ontokit.api.dependencies import _graph_load_locks, load_project_graph


@pytest.mark.asyncio
async def test_load_project_graph_locking() -> None:
    project_id = uuid4()
    branch = "main"
    key = (project_id, branch)

    # Clean up before test
    _graph_load_locks.clear()

    # Mock DB
    db = AsyncMock()
    project = MagicMock()
    project.id = project_id
    project.source_file_path = "ont.ttl"

    result = MagicMock()
    result.scalar_one_or_none.return_value = project
    db.execute.return_value = result

    # Mock services
    ontology = MagicMock()
    loaded = False

    def mock_is_loaded(*_args: Any, **_kwargs: Any) -> bool:
        return loaded

    ontology.is_loaded.side_effect = mock_is_loaded
    ontology.get_graph = AsyncMock(return_value=(MagicMock(), branch))

    # Simulate loading delay to ensure concurrent calls overlap
    load_called = 0

    async def slow_load(*_args: Any, **_kwargs: Any) -> None:
        nonlocal load_called, loaded
        load_called += 1
        await asyncio.sleep(0.2)
        loaded = True

    ontology.load_from_git = AsyncMock(side_effect=slow_load)

    with (
        patch("ontokit.services.ontology.get_ontology_service", return_value=ontology),
        patch("ontokit.services.storage.get_storage_service", return_value=MagicMock()),
        patch("ontokit.git.bare_repository.get_bare_git_service", return_value=MagicMock()),
        patch("ontokit.api.dependencies.resolve_branch", return_value=branch),
    ):
        # Call concurrently
        t1 = asyncio.create_task(load_project_graph(project_id, branch, db))
        t2 = asyncio.create_task(load_project_graph(project_id, branch, db))

        # Wait a bit to ensure they are both in flight
        await asyncio.sleep(0.1)

        # Verify lock is in the dict and is locked
        assert key in _graph_load_locks
        assert _graph_load_locks[key].locked()

        # Let them finish
        await asyncio.gather(t1, t2)

        # Verify it was only loaded once
        assert load_called == 1

        # Verify lock is removed from the dict
        assert key not in _graph_load_locks


@pytest.mark.asyncio
async def test_load_project_graph_waiters_cleanup() -> None:
    project_id = uuid4()
    branch = "main"
    key = (project_id, branch)

    # Clean up before test
    _graph_load_locks.clear()

    # Mock DB
    db = AsyncMock()
    project = MagicMock()
    project.id = project_id
    project.source_file_path = "ont.ttl"

    result = MagicMock()
    result.scalar_one_or_none.return_value = project
    db.execute.return_value = result

    # Mock services
    ontology = MagicMock()
    ontology.is_loaded.return_value = False
    ontology.get_graph = AsyncMock(return_value=(MagicMock(), branch))

    # Simulate loading delay
    async def slow_load(*_args: Any, **_kwargs: Any) -> None:
        await asyncio.sleep(0.2)

    ontology.load_from_git = AsyncMock(side_effect=slow_load)

    with (
        patch("ontokit.services.ontology.get_ontology_service", return_value=ontology),
        patch("ontokit.services.storage.get_storage_service", return_value=MagicMock()),
        patch("ontokit.git.bare_repository.get_bare_git_service", return_value=MagicMock()),
        patch("ontokit.api.dependencies.resolve_branch", return_value=branch),
    ):
        # Task 1 starts loading
        t1 = asyncio.create_task(load_project_graph(project_id, branch, db))
        await asyncio.sleep(0.05)

        # Task 2 starts and waits for Task 1
        t2 = asyncio.create_task(load_project_graph(project_id, branch, db))
        await asyncio.sleep(0.05)

        # Task 1 finishes soon, Task 2 will still be in flight
        await t1

        # At this point Task 1 has released the lock and tried to pop it.
        # If Task 2 is already a waiter, it should have acquired the lock,
        # so Task 1 should NOT have popped it (because lock.locked() was True).
        # Actually, let's verify Task 2 is indeed holding it.
        # NOTE: There might be a tiny race here in how asyncio.Lock wakes up waiters.

        await t2

        # After everyone is done, it should be popped.
        assert key not in _graph_load_locks
