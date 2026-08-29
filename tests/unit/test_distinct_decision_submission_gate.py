"""Submission-time duplicate gates honor only current matching decisions."""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from ontokit.services.suggestion_service import SuggestionService

BASELINE = b"""
@prefix ex: <https://example.test/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
ex:Existing a owl:Class ; rdfs:label "Legal Entity" .
"""

PROPOSED = """
@prefix ex: <https://example.test/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
ex:Existing a owl:Class ; rdfs:label "Legal Entity" .
ex:Minted a owl:Class ; rdfs:label "Legal Entity" .
"""


def _service() -> SuggestionService:
    git = MagicMock()
    git.get_default_branch.return_value = "main"
    git.get_file_from_branch.return_value = BASELINE
    return SuggestionService(MagicMock(), git)


@pytest.mark.asyncio
async def test_current_matching_decision_allows_exact_label_pair() -> None:
    decision = MagicMock(
        iri_a="https://example.test/Existing",
        iri_b="https://example.test/Minted",
    )
    check = AsyncMock(
        return_value=MagicMock(verdict="pass", suppressed_decisions=[decision])
    )
    service = _service()
    with patch(
        "ontokit.services.duplicate_check_service.DuplicateCheckService"
    ) as service_cls:
        service_cls.return_value.check = check
        await service._validate_submission_content(
            uuid4(), "suggest/editor/session", "ontology.ttl", PROPOSED, "editor-1"
        )

    assert check.await_args.kwargs["proposed_iri"] == "https://example.test/Minted"
    assert check.await_args.kwargs["entity_type"] == "class"
    assert check.await_args.kwargs["billing_user_id"] == "editor-1"


@pytest.mark.asyncio
async def test_stale_or_missing_decision_cannot_bypass_exact_label_gate() -> None:
    service = _service()
    check = AsyncMock(return_value=MagicMock(verdict="pass", suppressed_decisions=[]))
    with patch(
        "ontokit.services.duplicate_check_service.DuplicateCheckService"
    ) as service_cls:
        service_cls.return_value.check = check
        with pytest.raises(HTTPException) as exc_info:
            await service._validate_submission_content(
                uuid4(), "suggest/editor/session", "ontology.ttl", PROPOSED, "editor-1"
            )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "Suggestion duplicates an existing entity label"


@pytest.mark.asyncio
async def test_current_semantic_block_still_rejects_submission() -> None:
    proposed = PROPOSED.replace('"Legal Entity" .\nex:Minted', '"Existing" .\nex:Minted').replace(
        'ex:Minted a owl:Class ; rdfs:label "Legal Entity"',
        'ex:Minted a owl:Class ; rdfs:label "Related Entity"',
    )
    service = _service()
    check = AsyncMock(return_value=MagicMock(verdict="block", suppressed_decisions=[]))
    with patch(
        "ontokit.services.duplicate_check_service.DuplicateCheckService"
    ) as service_cls:
        service_cls.return_value.check = check
        with pytest.raises(HTTPException) as exc_info:
            await service._validate_submission_content(
                uuid4(), "suggest/editor/session", "ontology.ttl", proposed, "editor-1"
            )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "Suggestion is a semantic duplicate of an existing entity"


@pytest.mark.asyncio
async def test_anonymous_new_entity_cannot_trigger_unbilled_semantic_compute() -> None:
    service = _service()
    with (
        patch(
            "ontokit.services.duplicate_check_service.DuplicateCheckService"
        ) as service_cls,
        pytest.raises(HTTPException) as exc_info,
    ):
        await service._validate_submission_content(
            uuid4(), "suggest/anonymous/session", "ontology.ttl", PROPOSED, None
        )

    assert exc_info.value.status_code == 403
    service_cls.assert_not_called()
