"""Focused submission-time contracts for fingerprint-bound distinct decisions."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from ontokit.services.embedding_service import EmbeddingBudgetExceeded, EmbeddingPricingUnavailable
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
    check = AsyncMock(return_value=MagicMock(verdict="pass", suppressed_decisions=[decision]))

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
    check = AsyncMock(return_value=MagicMock(verdict="pass", suppressed_decisions=[MagicMock()]))

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
    check = AsyncMock(return_value=MagicMock(verdict="pass", suppressed_decisions=[]))

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


@pytest.mark.asyncio
async def test_individuals_do_not_expand_submission_validation_or_entity_cap() -> None:
    service, _git = _service()
    # More than 25 new individuals must not enter the class/property-only cap,
    # billing identity requirement, duplicate check, or namespace validation.
    proposed = BASELINE.decode() + "\n".join(
        f'ex:individual{i} a owl:NamedIndividual, ex:Person; rdfs:label "Existing" .'
        for i in range(26)
    )
    check = AsyncMock()
    validate = AsyncMock()
    namespace = AsyncMock()
    with (
        patch("ontokit.services.duplicate_check_service.DuplicateCheckService.check", new=check),
        patch(
            "ontokit.services.validation_service.ValidationService.validate_entity", new=validate
        ),
        patch("ontokit.services.validation_service.detect_project_namespace", new=namespace),
    ):
        await service._validate_submission_content(
            PROJECT_ID, "suggestion/test", "ontology.ttl", proposed, None
        )
    check.assert_not_awaited()
    validate.assert_not_awaited()
    namespace.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "status_code", "detail"),
    [
        (
            EmbeddingBudgetExceeded("Project embedding budget exceeded"),
            402,
            "Project embedding budget exceeded",
        ),
        (
            EmbeddingPricingUnavailable("internal model pricing credential reference"),
            503,
            "Embedding pricing is unavailable; duplicate check is paused.",
        ),
    ],
    ids=["budget", "pricing"],
)
async def test_submission_maps_embedding_failure(
    failure: RuntimeError, status_code: int, detail: str
) -> None:
    service, _git = _service()
    proposed = BASELINE.decode() + 'ex:Minted a owl:Class; rdfs:label "New class" .'
    check = AsyncMock(side_effect=failure)
    with (
        patch("ontokit.services.duplicate_check_service.DuplicateCheckService.check", new=check),
        pytest.raises(HTTPException) as exc,
    ):
        await service._validate_submission_content(
            PROJECT_ID, "suggestion/test", "ontology.ttl", proposed, "test-user-id"
        )
    assert exc.value.status_code == status_code
    assert exc.value.detail == detail
    assert exc.value.__cause__ is failure
    check.assert_awaited_once_with(
        PROJECT_ID,
        "New class",
        entity_type="class",
        parent_iri=None,
        billing_user_id="test-user-id",
        exclude_branch="suggestion/test",
        exclude_iris={"http://example.org/Minted"},
        proposed_iri="http://example.org/Minted",
    )


@pytest.mark.asyncio
async def test_submission_leaves_unknown_embedding_errors_unchanged() -> None:
    service, _git = _service()
    failure = RuntimeError("unexpected provider failure")
    proposed = BASELINE.decode() + 'ex:Minted a owl:Class; rdfs:label "New class" .'
    with (
        patch(
            "ontokit.services.duplicate_check_service.DuplicateCheckService.check",
            new=AsyncMock(side_effect=failure),
        ),
        pytest.raises(RuntimeError) as exc,
    ):
        await service._validate_submission_content(
            PROJECT_ID, "suggestion/test", "ontology.ttl", proposed, "test-user-id"
        )
    assert exc.value is failure


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_type", [EmbeddingBudgetExceeded, EmbeddingPricingUnavailable])
async def test_submission_stops_after_later_embedding_failure(
    failure_type: type[RuntimeError],
) -> None:
    service, _git = _service()
    proposed = BASELINE.decode() + "\n".join(
        f'ex:Minted{i} a owl:Class; rdfs:label "New class {i}" .' for i in range(3)
    )
    failure = failure_type("later check refused")
    check = AsyncMock(side_effect=[MagicMock(verdict="pass", suppressed_decisions=[]), failure])
    with (
        patch("ontokit.services.duplicate_check_service.DuplicateCheckService.check", new=check),
        pytest.raises(HTTPException) as exc,
    ):
        await service._validate_submission_content(
            PROJECT_ID, "suggestion/test", "ontology.ttl", proposed, "test-user-id"
        )
    assert exc.value.__cause__ is failure
    assert check.await_count == 2
    assert len({call.kwargs["proposed_iri"] for call in check.await_args_list}) == 2
    service.db.commit.assert_not_awaited()
    service.db.rollback.assert_not_awaited()
