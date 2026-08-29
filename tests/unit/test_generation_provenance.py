"""Sticky server-owned provenance for LLM-assisted suggestion sessions."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.api.routes.generation import _mark_active_session_llm_generated

PROJECT_ID = UUID("11111111-1111-1111-1111-111111111111")


@pytest.mark.asyncio
async def test_marker_is_scoped_to_active_project_user_and_branch() -> None:
    db = AsyncMock(spec=AsyncSession)
    result = MagicMock()
    result.rowcount = 1
    db.execute.return_value = result

    marked = await _mark_active_session_llm_generated(
        db, PROJECT_ID, "contributor-1", "suggest/s_123"
    )

    assert marked is True
    statement = db.execute.await_args.args[0]
    statement_text = str(statement)
    assert "suggestion_sessions.project_id" in statement_text
    assert "suggestion_sessions.user_id" in statement_text
    assert "suggestion_sessions.branch" in statement_text
    assert "suggestion_sessions.status" in statement_text
    assert statement.compile().params["is_llm_generated"] is True
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_matching_active_session_is_harmless() -> None:
    db = AsyncMock(spec=AsyncSession)
    result = MagicMock()
    result.rowcount = 0
    db.execute.return_value = result

    marked = await _mark_active_session_llm_generated(db, PROJECT_ID, "user-1", "main")

    assert marked is False
    db.commit.assert_not_awaited()
