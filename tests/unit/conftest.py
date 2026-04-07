"""Unit-test fixtures for Phase 13 — validation guardrails and suggestion generation."""

from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def mock_llm_provider():
    """AsyncMock for LLMProvider.chat() returning (json_string, input_tokens, output_tokens)."""
    provider = AsyncMock()
    provider.chat = AsyncMock(return_value=('{"suggestions": []}', 100, 50))
    return provider


@pytest.fixture
def mock_ontology_index():
    """AsyncMock for OntologyIndexService with get_class_detail, get_class_children, get_ancestor_path."""
    index = AsyncMock()
    index.get_class_detail = AsyncMock(return_value=None)
    index.get_class_children = AsyncMock(return_value=[])
    index.get_ancestor_path = AsyncMock(return_value=[])
    return index


@pytest.fixture
def mock_duplicate_check_service():
    """AsyncMock for DuplicateCheckService.check() returning a pass verdict."""
    svc = AsyncMock()
    svc.check = AsyncMock(
        return_value=AsyncMock(
            verdict="pass",
            composite_score=0.0,
        )
    )
    return svc
