"""Tests for the paid embedding-query authorization boundary."""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from ontokit.api.llm_access import require_embedding_query_access
from ontokit.core.auth import CurrentUser


@pytest.mark.asyncio
async def test_editor_consumes_project_user_rate_limit() -> None:
    project_id = uuid4()
    user = CurrentUser(id="user-1")
    redis = MagicMock()
    limiter = AsyncMock(return_value=True)

    with (
        patch(
            "ontokit.api.llm_access.get_rate_limit_redis",
            return_value=redis,
        ),
        patch(
            "ontokit.api.llm_access.check_rate_limit",
            new=limiter,
        ),
    ):
        role = await require_embedding_query_access(project_id, user, "editor")

    assert role == "editor"
    limiter.assert_awaited_once_with(redis, str(project_id), "user-1", "editor")


@pytest.mark.asyncio
async def test_viewer_is_denied_before_rate_limit() -> None:
    user = CurrentUser(id="viewer-1")

    with (
        patch("ontokit.api.llm_access.get_rate_limit_redis") as get_redis,
        pytest.raises(HTTPException) as exc_info,
    ):
        await require_embedding_query_access(uuid4(), user, "viewer")

    assert exc_info.value.status_code == 403
    get_redis.assert_not_called()


@pytest.mark.asyncio
async def test_exhausted_editor_is_rate_limited() -> None:
    user = CurrentUser(id="user-1")

    with (
        patch(
            "ontokit.api.llm_access.get_rate_limit_redis",
            return_value=MagicMock(),
        ),
        patch(
            "ontokit.api.llm_access.check_rate_limit",
            new=AsyncMock(return_value=False),
        ),
        pytest.raises(HTTPException) as exc_info,
    ):
        await require_embedding_query_access(uuid4(), user, "editor")

    assert exc_info.value.status_code == 429


@pytest.mark.asyncio
async def test_missing_pool_fails_open_with_alert(caplog: pytest.LogCaptureFixture) -> None:
    user = CurrentUser(id="user-1")

    with patch(
        "ontokit.api.llm_access.get_rate_limit_redis",
        return_value=None,
    ):
        role = await require_embedding_query_access(uuid4(), user, "suggester")

    assert role == "suggester"
    alerts = [
        record
        for record in caplog.records
        if getattr(record, "operation", None) == "embedding_query_rate_limit_bypass"
    ]
    assert len(alerts) == 1
