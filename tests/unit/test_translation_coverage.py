"""Coverage and provenance query behavior for translations."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from rdflib import Graph, Literal, Namespace
from rdflib.namespace import SKOS

from ontokit.api.routes.translation import get_translation_coverage
from ontokit.core.auth import CurrentUser
from ontokit.models.translation import TranslationRecord, hash_literal_value
from ontokit.services.translation_annotations import (
    TranslationAnnotation,
    annotate,
    translation_record_digest,
)
from ontokit.services.translation_coverage import LabelValue, TranslationCoverageService

PROJECT_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
EX = Namespace("https://example.org/")
CREATED = datetime(2026, 8, 9, 12, tzinfo=UTC)


def _record(
    entity: str,
    language: str,
    source: str,
    translated: str,
    *,
    state: str = "verified",
) -> TranslationRecord:
    return TranslationRecord(
        id=uuid.uuid4(),
        project_id=PROJECT_ID,
        entity_iri=entity,
        predicate=str(SKOS.prefLabel),
        language=language,
        source_value=source,
        proposed_value=translated,
        source_value_hash=hash_literal_value(source),
        translated_value_hash=hash_literal_value(translated),
        model_name="translator",
        model_version="1",
        method="consensus",
        score=0.94,
        state=state,
        created_at=CREATED,
    )


def _annotate(graph: Graph, record: TranslationRecord, value: str) -> None:
    literal = Literal(value, lang=record.language)
    graph.add((EX[record.entity_iri.rsplit("/", 1)[-1]], SKOS.prefLabel, literal))
    annotate(
        graph,
        EX[record.entity_iri.rsplit("/", 1)[-1]],
        SKOS.prefLabel,
        literal,
        TranslationAnnotation(
            method=record.method,
            state=record.state,
            created=CREATED,
            record_digest=translation_record_digest(record),
        ),
    )


def _labels() -> list[LabelValue]:
    return [
        LabelValue(str(EX.one), str(SKOS.prefLabel), "en", "One"),
        LabelValue(str(EX.two), str(SKOS.prefLabel), "en", "Two"),
    ]


def test_coverage_matrix_has_verified_provisional_and_missing() -> None:
    verified = _record(str(EX.one), "es", "One", "Uno")
    provisional = _record(str(EX.two), "fr", "Two", "Deux", state="provisional")
    graph = Graph()
    _annotate(graph, verified, "Uno")
    labels = _labels() + [LabelValue(str(EX.one), str(SKOS.prefLabel), "es", "Uno")]

    result = TranslationCoverageService.classify(
        branch="main",
        languages=["es", "fr", "de"],
        labels=labels,
        records=[verified, provisional],
        graph=graph,
    )

    assert result == {
        "branch": "main",
        "languages": [
            {
                "language": "es",
                "verified": 1,
                "provisional": 0,
                "pending": 0,
                "missing": 1,
                "total": 2,
            },
            {
                "language": "fr",
                "verified": 0,
                "provisional": 1,
                "pending": 0,
                "missing": 1,
                "total": 2,
            },
            {
                "language": "de",
                "verified": 0,
                "provisional": 0,
                "pending": 0,
                "missing": 2,
                "total": 2,
            },
        ],
        "total_entities": 2,
    }


def test_orphaned_record_and_unannotated_human_literal_are_missing() -> None:
    orphan = _record(str(EX.one), "es", "Old source", "Uno")
    coincidental = _record(str(EX.two), "es", "Two", "Dos")
    graph = Graph()
    graph.add((EX.two, SKOS.prefLabel, Literal("Dos", lang="es")))
    labels = _labels() + [LabelValue(str(EX.two), str(SKOS.prefLabel), "es", "Dos")]

    result = TranslationCoverageService.classify(
        branch="main", languages=["es"], labels=labels, records=[orphan, coincidental], graph=graph
    )

    assert result["languages"][0]["missing"] == 2
    assert result["languages"][0]["verified"] == 0


def test_annotation_is_branch_agnostic_when_present_on_each_branch() -> None:
    record = _record(str(EX.one), "es", "One", "Uno")
    for branch in ("branch-a", "branch-b"):
        graph = Graph()
        _annotate(graph, record, "Uno")
        labels = _labels()[:1] + [LabelValue(str(EX.one), str(SKOS.prefLabel), "es", "Uno")]
        result = TranslationCoverageService.classify(
            branch=branch, languages=["es"], labels=labels, records=[record], graph=graph
        )
        assert result["languages"][0]["verified"] == 1


def test_active_job_scope_is_pending() -> None:
    result = TranslationCoverageService.classify(
        branch="main",
        languages=["es"],
        labels=_labels()[:1],
        records=[],
        graph=Graph(),
        pending={(str(EX.one), str(SKOS.prefLabel), "es")},
    )

    assert result["languages"][0] == {
        "language": "es",
        "verified": 0,
        "provisional": 0,
        "pending": 1,
        "missing": 0,
        "total": 1,
    }


def test_entity_state_and_provisional_queue_return_required_values() -> None:
    verified = _record(str(EX.one), "es", "One", "Uno")
    provisional = _record(str(EX.one), "fr", "One", "Un", state="provisional")
    graph = Graph()
    _annotate(graph, verified, "Uno")
    labels = _labels()[:1] + [LabelValue(str(EX.one), str(SKOS.prefLabel), "es", "Uno")]
    service = TranslationCoverageService(None, None)  # pure projection helpers need no I/O

    items = service.entity_state_from_data(
        str(EX.one), "main", ["es", "fr", "de"], labels, [verified, provisional], graph
    )
    assert items["items"] == [
        {
            "predicate": str(SKOS.prefLabel),
            "language": "es",
            "state": "verified",
            "value": None,
            "record_id": str(verified.id),
        },
        {
            "predicate": str(SKOS.prefLabel),
            "language": "fr",
            "state": "provisional",
            "value": "Un",
            "record_id": str(provisional.id),
        },
        {
            "predicate": str(SKOS.prefLabel),
            "language": "de",
            "state": "missing",
            "value": None,
            "record_id": None,
        },
    ]
    queue = service.provisional_from_data("fr", _labels(), [provisional])
    assert queue[0]["source_value"] == "One"
    assert queue[0]["proposed_value"] == "Un"


@pytest.mark.asyncio
@pytest.mark.parametrize("user", [None, CurrentUser(id="outsider")])
async def test_private_project_unauthenticated_or_non_member_is_denied(
    mock_db_session: AsyncMock, user: CurrentUser | None
) -> None:
    project_result = Mock()
    project_result.scalar_one_or_none.return_value = Mock(is_public=False)
    member_result = Mock()
    member_result.scalar_one_or_none.return_value = None
    mock_db_session.execute.side_effect = [project_result, member_result]

    with pytest.raises(HTTPException) as exc:
        await get_translation_coverage(PROJECT_ID, "main", mock_db_session, user, Mock())
    assert exc.value.status_code == 403
