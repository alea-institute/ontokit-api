"""Unit test fixtures."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.core.auth import CurrentUser, get_current_user, get_current_user_optional
from ontokit.core.database import get_db
from ontokit.main import app
from ontokit.schemas.duplicate_check import DuplicateCheckResponse, ScoreBreakdown


@pytest.fixture
def authed_client() -> Generator[tuple[TestClient, AsyncMock], None, None]:
    """TestClient with mocked DB and authenticated user.

    Returns (client, mock_session) so tests can configure DB responses.
    """
    mock_session = AsyncMock(spec=AsyncSession)
    mock_session.commit = AsyncMock()
    mock_session.rollback = AsyncMock()
    mock_session.close = AsyncMock()
    mock_session.execute = AsyncMock()
    mock_session.refresh = AsyncMock()
    mock_session.add = lambda _x: None  # sync method
    mock_session.delete = AsyncMock()

    user = CurrentUser(
        id="test-user-id",
        email="test@example.com",
        name="Test User",
        username="testuser",
        roles=["owner"],
    )

    async def _override_get_db() -> Any:
        yield mock_session

    async def _override_get_current_user() -> CurrentUser:
        return user

    async def _override_get_current_user_optional() -> CurrentUser | None:
        return user

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = _override_get_current_user
    app.dependency_overrides[get_current_user_optional] = _override_get_current_user_optional

    client = TestClient(app, raise_server_exceptions=False)
    yield client, mock_session

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Suggestion generation fixtures (Phase 13 / PR-5)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_llm_provider() -> AsyncMock:
    """AsyncMock for LLMProvider.chat() returning (json_string, input_tokens, output_tokens)."""
    provider = AsyncMock()
    provider.chat = AsyncMock(return_value=('{"suggestions": []}', 100, 50))
    return provider


@pytest.fixture
def mock_ontology_index() -> AsyncMock:
    """AsyncMock for OntologyIndexService with get_class_detail/children/ancestor_path."""
    index = AsyncMock()
    index.get_class_detail = AsyncMock(return_value=None)
    index.get_class_children = AsyncMock(return_value=[])
    index.get_ancestor_path = AsyncMock(return_value=[])
    return index


@pytest.fixture
def mock_duplicate_check_service() -> AsyncMock:
    """AsyncMock for DuplicateCheckService batch checks returning pass verdicts."""
    svc = AsyncMock()
    response = DuplicateCheckResponse(
        verdict="pass",
        composite_score=0.0,
        score_breakdown=ScoreBreakdown(exact=0.0, semantic=0.0, structural=0.0),
        candidates=[],
    )
    svc.check = AsyncMock(return_value=response)
    svc.check_many = AsyncMock(
        side_effect=lambda _project_id, checks: [response.model_copy(deep=True) for _ in checks]
    )
    return svc
