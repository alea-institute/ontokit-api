"""Focused submission-time contracts for fingerprint-bound distinct decisions."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ontokit.services.suggestion_service import SuggestionService

PROJECT_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
BASELINE = b"""
    @prefix ex: <http://example.org/> .
    @prefix owl: <http://www.w3.org/2002/07/owl#> .
    @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
    ex:Existing a owl:Class ; rdfs:label "Existing" .
"""


def _service() -> tuple[SuggestionService, MagicMock]:
    git = MagicMock()
    git.get_default_branch.return_value = "main"
    git.get_file_from_branch.return_value = BASELINE
    return SuggestionService(AsyncMock(), git), git


@pytest.mark.asyncio
async def test_marked_distinct_exact_label_pair_can_submit() -> None:
    service, _git = _service()
    proposed = """
        @prefix ex: <http://example.org/> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
        ex:Existing a owl:Class ; rdfs:label "Existing" .
        ex:Minted a owl:Class ; rdfs:label "Existing" .
    """
    decision = MagicMock(
        iri_a="http://example.org/Existing",
        iri_b="http://example.org/Minted",
    )
    check = AsyncMock(
        return_value=MagicMock(verdict="pass", suppressed_decisions=[decision])
    )

    with (
        patch(
            "ontokit.services.duplicate_check_service.DuplicateCheckService.check",
            new=check,
        ),
        patch(
            "ontokit.services.validation_service.detect_project_namespace",
            new=AsyncMock(return_value="http://example.org/"),
        ),
        patch(
            "ontokit.services.validation_service.ValidationService.validate_entity",
            new=AsyncMock(return_value=[]),
        ),
    ):
        await service._validate_submission_content(
            PROJECT_ID,
            "suggestion/test",
            "ontology.ttl",
            proposed,
            "test-user-id",
        )

    assert check.await_args.kwargs["proposed_iri"] == "http://example.org/Minted"


@pytest.mark.asyncio
async def test_marked_distinct_semantic_pair_can_submit() -> None:
    service, _git = _service()
    proposed = """
        @prefix ex: <http://example.org/> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
        ex:Existing a owl:Class ; rdfs:label "Existing" .
        ex:Minted a owl:Class ; rdfs:label "Closely Related" .
    """
    check = AsyncMock(
        return_value=MagicMock(verdict="pass", suppressed_decisions=[MagicMock()])
    )

    with (
        patch(
            "ontokit.services.duplicate_check_service.DuplicateCheckService.check",
            new=check,
        ),
        patch(
            "ontokit.services.validation_service.detect_project_namespace",
            new=AsyncMock(return_value="http://example.org/"),
        ),
        patch(
            "ontokit.services.validation_service.ValidationService.validate_entity",
            new=AsyncMock(return_value=[]),
        ),
    ):
        await service._validate_submission_content(
            PROJECT_ID,
            "suggestion/test",
            "ontology.ttl",
            proposed,
            "test-user-id",
        )

    assert check.await_args.kwargs["proposed_iri"] == "http://example.org/Minted"


@pytest.mark.asyncio
async def test_property_submission_preserves_type_for_distinct_fingerprint() -> None:
    service, _git = _service()
    proposed = """
        @prefix ex: <http://example.org/> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
        ex:Existing a owl:Class ; rdfs:label "Existing" .
        ex:MintedProperty a owl:ObjectProperty ; rdfs:label "Closely Related" .
    """
    check = AsyncMock(
        return_value=MagicMock(verdict="pass", suppressed_decisions=[])
    )

    with patch(
        "ontokit.services.duplicate_check_service.DuplicateCheckService.check",
        new=check,
    ):
        await service._validate_submission_content(
            PROJECT_ID,
            "suggestion/test",
            "ontology.ttl",
            proposed,
            "test-user-id",
        )

    assert check.await_args.kwargs["entity_type"] == "property"
    assert check.await_args.kwargs["proposed_iri"] == "http://example.org/MintedProperty"
