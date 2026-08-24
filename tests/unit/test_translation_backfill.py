"""Proof-first coverage for translation backfill and cost preview (U7)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import HTTPException
from rdflib import Graph
from rdflib.namespace import SKOS

from ontokit.api.routes.translation import launch_translation_backfill
from ontokit.core.auth import CurrentUser
from ontokit.models.translation import TranslationJob, TranslationRecord, hash_literal_value
from ontokit.schemas.translation import TranslationBackfillRequest
from ontokit.services.translation_backfill import (
    BackfillLiteral,
    preview_backfill_cost,
    select_backfill_literals,
)
from ontokit.services.translation_coverage import LabelValue, TranslationCoverageService
from ontokit.services.translation_jobs import run_translation_backfill_job

PROJECT_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")


def _literal(language: str = "fr") -> BackfillLiteral:
    return BackfillLiteral(
        entity_iri="https://example.test/cat",
        predicate=str(SKOS.prefLabel),
        source_value="Cat",
        source_language="en",
        target_language=language,
        context_labels=("Feline",),
    )


@pytest.mark.asyncio
async def test_preview_prices_each_call_without_spending() -> None:
    pricing = AsyncMock(side_effect=[(0.001, 0.004), (0.002, 0.008)])
    provider = AsyncMock()
    audit = AsyncMock()

    preview = await preview_backfill_cost(
        [_literal()],
        mechanism="confidence",
        primary_model="primary",
        verifier_model="verifier",
        primary_provider="google",
        speed_mode="batch",
        pricing_resolver=pricing,
    )

    assert preview.literal_count == 1
    assert preview.expected_cost_usd > 0
    assert preview.upper_bound_cost_usd > preview.expected_cost_usd
    assert preview.batch_discount_applied is False
    provider.chat.assert_not_awaited()
    audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_batch_discount_only_for_true_batch_provider_and_separate_prices() -> None:
    prices = AsyncMock(return_value=(0.001, 0.010))
    standard = await preview_backfill_cost(
        [_literal()],
        mechanism="consensus",
        primary_model="model",
        verifier_model="model",
        primary_provider="openai",
        speed_mode="fast",
        pricing_resolver=prices,
    )
    batch = await preview_backfill_cost(
        [_literal()],
        mechanism="consensus",
        primary_model="model",
        verifier_model="model",
        primary_provider="openai",
        speed_mode="batch",
        pricing_resolver=prices,
        batch_capable_providers=frozenset({"openai"}),
    )
    unsupported = await preview_backfill_cost(
        [_literal()],
        mechanism="consensus",
        primary_model="model",
        verifier_model="model",
        primary_provider="cohere",
        speed_mode="batch",
        pricing_resolver=prices,
    )

    assert batch.expected_cost_usd == pytest.approx(standard.expected_cost_usd * 0.5)
    assert batch.batch_discount_applied is True
    assert unsupported.expected_cost_usd == pytest.approx(standard.expected_cost_usd)
    assert unsupported.batch_discount_applied is False


@pytest.mark.asyncio
async def test_second_launch_while_active_is_conflict() -> None:
    db = AsyncMock()
    active_result = Mock()
    active_result.scalar_one_or_none.return_value = TranslationJob(
        id=uuid.uuid4(), project_id=PROJECT_ID, branch="main", status="running"
    )
    db.execute.return_value = active_result
    user = CurrentUser(id="user-1", email="user@example.test", is_superadmin=True)

    with (
        patch("ontokit.api.routes.translation._require_owner_or_admin", AsyncMock()),
        pytest.raises(HTTPException) as exc,
    ):
        await launch_translation_backfill(
            PROJECT_ID, TranslationBackfillRequest(branch="main"), db, user
        )

    assert exc.value.status_code == 409


def test_active_project_job_marks_all_in_scope_literals_pending() -> None:
    job = TranslationJob(
        id=uuid.uuid4(),
        project_id=PROJECT_ID,
        branch="main",
        status="running",
        language="fr",
    )
    scopes = TranslationCoverageService.pending_scopes_from_jobs(
        [job],
        {
            ("https://example.test/cat", str(SKOS.prefLabel), "fr"),
            ("https://example.test/cat", str(SKOS.prefLabel), "de"),
        },
    )
    assert scopes == {("https://example.test/cat", str(SKOS.prefLabel), "fr")}


@pytest.mark.asyncio
async def test_failure_records_progress_and_relaunch_skips_completed_work() -> None:
    job = TranslationJob(
        id=uuid.uuid4(),
        project_id=PROJECT_ID,
        branch="main",
        status="pending",
        total_literals=0,
        completed_literals=0,
    )
    db = AsyncMock()
    db.get.return_value = job
    tasks = [_literal("fr"), _literal("de")]
    runner = AsyncMock(side_effect=[None, RuntimeError("provider failed")])

    with (
        patch(
            "ontokit.services.translation_jobs.select_backfill_literals",
            AsyncMock(return_value=tasks),
        ),
        pytest.raises(RuntimeError, match="provider failed"),
    ):
        await run_translation_backfill_job(
            {"db": db}, str(PROJECT_ID), "main", str(job.id), "actor", task_runner=runner
        )

    assert job.status == "failed"
    assert job.completed_literals == 1
    assert job.error_message == "provider failed"
    assert db.commit.await_count >= 2


@pytest.mark.asyncio
async def test_completed_backfill_redelivery_spends_zero_provider_calls() -> None:
    job = TranslationJob(
        id=uuid.uuid4(),
        project_id=PROJECT_ID,
        branch="main",
        status="completed",
        total_literals=2,
        completed_literals=2,
    )
    db = AsyncMock()
    db.get.return_value = job
    runner = AsyncMock()
    with patch(
        "ontokit.services.translation_jobs.select_backfill_literals", AsyncMock()
    ) as select_literals:
        result = await run_translation_backfill_job(
            {"db": db}, str(PROJECT_ID), "main", str(job.id), "actor", task_runner=runner
        )
    assert result["completed"] == 2
    select_literals.assert_not_awaited()
    runner.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_registered_backfill_wrapper_delegates_lifecycle_payload() -> None:
    from ontokit import worker

    registered = next(
        function
        for function in worker.WorkerSettings.functions
        if getattr(function, "__name__", "") == "run_translation_backfill_task"
    )
    expected = {"job_id": "job", "completed": 0}
    with patch.object(
        worker, "run_translation_backfill_job", AsyncMock(return_value=expected)
    ) as run:
        result = await registered({}, str(PROJECT_ID), "main", "job", "actor")
    assert result == expected
    run.assert_awaited_once_with({}, str(PROJECT_ID), "main", "job", "actor")


def test_translation_job_active_index_is_project_scoped() -> None:
    index = next(index for index in TranslationJob.__table__.indexes if index.unique)
    assert [column.name for column in index.columns] == ["project_id"]
    assert "pending" in str(index.dialect_options["postgresql"]["where"])


@pytest.mark.asyncio
async def test_new_language_scope_preserves_untagged_source_identity() -> None:
    labels = [LabelValue("https://example.test/cat", str(SKOS.prefLabel), None, "Cat")]
    with patch.object(
        TranslationCoverageService,
        "_load",
        AsyncMock(return_value=(["fr", "de"], labels, [], Graph())),
    ):
        selected = await select_backfill_literals(
            AsyncMock(), Mock(), PROJECT_ID, "main", language="fr"
        )

    assert [literal.target_language for literal in selected] == ["fr"]
    assert selected[0].source_language is None


@pytest.mark.asyncio
async def test_era_scope_excludes_native_confirmed_records() -> None:
    cutoff = datetime(2027, 1, 1, tzinfo=UTC)
    machine = TranslationRecord(
        id=uuid.uuid4(),
        project_id=PROJECT_ID,
        entity_iri="https://example.test/cat",
        predicate=str(SKOS.prefLabel),
        language="fr",
        source_value="Cat",
        proposed_value="Chat",
        source_value_hash=hash_literal_value("Cat"),
        translated_value_hash=hash_literal_value("Chat"),
        model_name="model",
        model_version="1",
        method="consensus",
        score=0.9,
        state="verified",
        confirmed_at=None,
    )
    native = TranslationRecord(
        id=uuid.uuid4(),
        project_id=PROJECT_ID,
        entity_iri="https://example.test/dog",
        predicate=str(SKOS.prefLabel),
        language="fr",
        source_value="Dog",
        proposed_value="Chien",
        source_value_hash=hash_literal_value("Dog"),
        translated_value_hash=hash_literal_value("Chien"),
        model_name="model",
        model_version="1",
        method="consensus",
        score=0.9,
        state="verified",
        confirmed_at=cutoff,
    )
    db = AsyncMock()
    result = Mock()
    result.scalars.return_value.all.return_value = [machine, native]
    db.execute.return_value = result
    with patch.object(
        TranslationCoverageService,
        "_load",
        AsyncMock(
            return_value=(
                ["fr"],
                [],
                [],
                Graph().parse(
                    data=(
                        f'@prefix skos: <{SKOS}> . <{machine.entity_iri}> skos:prefLabel "Cat"@en .'
                    ),
                    format="turtle",
                ),
            )
        ),
    ):
        selected = await select_backfill_literals(
            db,
            Mock(),
            PROJECT_ID,
            "main",
            era_before=cutoff,
            never_confirmed=True,
        )

    assert [literal.entity_iri for literal in selected] == [machine.entity_iri]
