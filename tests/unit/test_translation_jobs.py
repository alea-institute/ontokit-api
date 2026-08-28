"""Proof-first coverage for mint-triggered translation jobs (U6)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDFS, SKOS

from ontokit.api.routes.translation import translate_entity_field
from ontokit.core.auth import CurrentUser
from ontokit.models.translation import ProjectTranslationConfig
from ontokit.schemas.translation import TranslateFieldRequest
from ontokit.services.llm.rate_limiter import RateLimitReservation
from ontokit.services.translation_jobs import (
    MAX_PENDING_TRANSLATIONS_PER_PROJECT,
    RELEASE_RECEIPT_TTL_SECONDS,
    TranslationTask,
    _provider_call_units,
    discover_translation_tasks,
    enqueue_label_diff_after_commit,
    enqueue_translation_tasks,
    run_translation_entity_job,
)

EX = Namespace("https://example.test/")


def _graph(*triples: tuple[URIRef, URIRef, Literal]) -> Graph:
    graph = Graph()
    for triple in triples:
        graph.add(triple)
    return graph


def _config(**overrides: object) -> ProjectTranslationConfig:
    values = {
        "project_id": uuid4(),
        "language_tags": ["fr", "de", "es"],
        "translate_definitions": False,
        "translate_examples": False,
        "speed_mode": "batch",
    }
    values.update(overrides)
    return ProjectTranslationConfig(**values)


def test_mint_pref_and_alt_label_fans_out_to_every_language() -> None:
    current = _graph(
        (EX.cat, SKOS.prefLabel, Literal("Cat", lang="en")),
        (EX.cat, SKOS.altLabel, Literal("Feline", lang="en")),
    )

    tasks = discover_translation_tasks(Graph(), current, _config(), covered=set())

    assert len(tasks) == 6
    assert {(task.predicate, task.target_language) for task in tasks} == {
        (str(predicate), language)
        for predicate in (SKOS.prefLabel, SKOS.altLabel)
        for language in ("fr", "de", "es")
    }
    assert {task.mode for task in tasks} == {"batch"}


@pytest.mark.parametrize(
    ("parent", "current"),
    [
        (
            _graph((EX.cat, RDFS.label, Literal("Cat", lang="en"))),
            _graph(
                (EX.cat, RDFS.label, Literal("Cat", lang="en")),
                (EX.cat, RDFS.comment, Literal("Unrelated", lang="en")),
            ),
        ),
        (
            _graph((EX.cat, RDFS.label, Literal("Cat", lang="en"))),
            _graph((EX.cat, RDFS.label, Literal("Cat", lang="en"))),
        ),
        (
            _graph((EX.cat, RDFS.label, Literal("Cat", lang="en"))),
            _graph((EX.cat, RDFS.label, Literal("Domestic cat", lang="en"))),
        ),
    ],
)
def test_non_label_unchanged_and_edited_label_changes_do_not_enqueue(
    parent: Graph, current: Graph
) -> None:
    assert discover_translation_tasks(parent, current, _config(), covered=set()) == []


def test_matching_hash_coverage_short_circuits_and_empty_languages_are_noop() -> None:
    current = _graph((EX.cat, RDFS.label, Literal("Cat", lang="en")))
    tasks = discover_translation_tasks(
        Graph(),
        current,
        _config(),
        covered={(str(EX.cat), str(RDFS.label), "Cat", "fr")},
    )
    assert {task.target_language for task in tasks} == {"de", "es"}
    assert discover_translation_tasks(Graph(), current, _config(language_tags=[]), set()) == []


def test_untagged_source_literal_keeps_rdf_language_identity() -> None:
    current = _graph((EX.cat, RDFS.label, Literal("Cat")))

    tasks = discover_translation_tasks(
        Graph(), current, _config(language_tags=["und", "fr"]), covered=set()
    )

    assert len(tasks) == 1
    assert tasks[0].source_language is None
    assert tasks[0].target_language == "fr"


def test_definition_scope_off_is_skipped() -> None:
    current = _graph((EX.cat, SKOS.definition, Literal("A small mammal", lang="en")))
    assert discover_translation_tasks(Graph(), current, _config(), covered=set()) == []


def test_provider_call_units_cover_every_fanned_out_provider_call() -> None:
    assert _provider_call_units(_config(verification_mechanism="consensus"), 3) == 12
    assert _provider_call_units(_config(verification_mechanism="confidence"), 3) == 6


def test_release_receipt_outlives_retries_but_not_arq_result_retention() -> None:
    assert 5 * 5 * 60 < RELEASE_RECEIPT_TTL_SECONDS < 60 * 60


@pytest.mark.asyncio
async def test_enqueue_attributes_actor_and_stops_at_fanout_cap() -> None:
    redis = AsyncMock()
    redis.incrby.return_value = MAX_PENDING_TRANSLATIONS_PER_PROJECT + 1
    pool = AsyncMock()
    task = TranslationTask(str(EX.cat), str(RDFS.label), "Cat", "en", "fr", "fast")

    with pytest.raises(RuntimeError, match="fan-out cap"):
        await enqueue_translation_tasks(pool, redis, uuid4(), "main", "actor-1", [task])

    pool.enqueue_job.assert_not_awaited()
    redis.decrby.assert_awaited_once()


@pytest.mark.asyncio
async def test_entity_job_identity_is_scoped_to_project_and_branch() -> None:
    redis = AsyncMock()
    redis.incrby.return_value = 1
    pool = AsyncMock()
    pool.enqueue_job.return_value = None
    task = TranslationTask(str(EX.cat), str(RDFS.label), "Cat", "en", "fr", "fast")
    first_project, second_project = uuid4(), uuid4()

    await enqueue_translation_tasks(pool, redis, first_project, "main", "actor-1", [task])
    await enqueue_translation_tasks(pool, redis, second_project, "main", "actor-1", [task])
    await enqueue_translation_tasks(pool, redis, first_project, "feature", "actor-1", [task])

    job_ids = [call.kwargs["_job_id"] for call in pool.enqueue_job.await_args_list]
    assert len(set(job_ids)) == 3
    # A duplicate enqueue owns no second pending slot.
    assert redis.decrby.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("first_release", [1, 0])
async def test_entity_job_uses_und_for_provider_but_none_for_rdf_match(
    first_release: int,
) -> None:
    project_id = uuid4()
    config = _config(project_id=project_id, language_tags=["fr"], primary_model="model")
    db = AsyncMock()
    db.scalar.side_effect = [
        config,
        Mock(),
        SimpleNamespace(github_integration=None, source_file_path=None),
    ]
    git = Mock()
    git.get_file_from_branch.return_value = (
        f'@prefix skos: <{SKOS}> .\n<{EX.cat}> skos:definition "An animal" .\n'
    )
    service = Mock()
    service.translate = AsyncMock(return_value={})
    service.apply_results = AsyncMock(return_value=SimpleNamespace(commit=None))
    redis = AsyncMock()
    redis.sadd.return_value = first_release

    with (
        patch("ontokit.services.translation_jobs.get_git_service", return_value=git),
        patch("ontokit.services.translation_jobs.TranslationService", return_value=service),
    ):
        await run_translation_entity_job(
            {"db": db, "redis": redis, "job_id": "job-1"},
            str(project_id),
            "main",
            str(EX.cat),
            str(SKOS.definition),
            None,
            None,
            "fr",
            "actor-1",
            "fast",
        )

    service.translate.assert_awaited_once_with("An animal", "und", ["fr"])
    assert service.apply_results.await_args.kwargs["source_language"] is None
    if first_release:
        redis.decrby.assert_awaited_once()
        redis.expire.assert_awaited_once_with(
            f"translation:released:{project_id}", RELEASE_RECEIPT_TTL_SECONDS
        )
    else:
        redis.decrby.assert_not_awaited()
        redis.expire.assert_not_awaited()


@pytest.mark.asyncio
async def test_label_diff_enqueue_is_deterministic_and_carries_actor_role() -> None:
    pool = AsyncMock()
    pool.enqueue_job.side_effect = [Mock(job_id="diff-1"), None]
    project_id = uuid4()

    with patch("ontokit.api.utils.redis.get_arq_pool", AsyncMock(return_value=pool)):
        accepted = await enqueue_label_diff_after_commit(
            project_id=project_id,
            branch="main",
            commit_hash="abc",
            actor_id="actor-1",
            role="editor",
        )
        duplicate = await enqueue_label_diff_after_commit(
            project_id=project_id,
            branch="main",
            commit_hash="abc",
            actor_id="actor-1",
            role="editor",
        )

    assert accepted is True
    assert duplicate is False
    first, second = pool.enqueue_job.await_args_list
    assert (
        first.args[:6]
        == second.args[:6]
        == (
            "run_translation_label_diff_task",
            str(project_id),
            "main",
            "abc",
            "actor-1",
            "editor",
        )
    )
    assert first.kwargs["_job_id"] == second.kwargs["_job_id"]


def test_translation_jobs_are_registered_with_arq_worker() -> None:
    from ontokit.worker import WorkerSettings

    names = {getattr(function, "__name__", "") for function in WorkerSettings.functions}
    assert "run_translation_label_diff_task" in names
    assert "run_translation_entity_task" in names
    assert "run_translation_backfill_task" in names


@pytest.mark.asyncio
async def test_on_demand_endpoint_is_role_gated_rate_limited_and_enqueues() -> None:
    project_id = uuid4()
    request = TranslateFieldRequest(
        entity_iri=str(EX.cat), predicate="skos:definition", branch="main"
    )
    db = AsyncMock()
    pool = AsyncMock()
    pool.enqueue_job.return_value = Mock(job_id="translation-1")
    user = CurrentUser(id="actor-1", name="Actor", email="actor@example.test")

    with (
        patch("ontokit.api.routes.translation._require_member", AsyncMock(return_value="viewer")),
        pytest.raises(HTTPException) as forbidden,
    ):
        await translate_entity_field(project_id, request, db, user)
    assert forbidden.value.status_code == 403

    with (
        patch("ontokit.api.routes.translation._require_member", AsyncMock(return_value="editor")),
        patch(
            "ontokit.api.routes.translation._get_config",
            AsyncMock(
                return_value=_config(language_tags=["fr"], verification_mechanism="confidence")
            ),
        ),
        patch("ontokit.api.routes.translation._get_redis", return_value=AsyncMock()),
        patch(
            "ontokit.api.routes.translation.reserve_rate_limit_units",
            AsyncMock(return_value=RateLimitReservation(accepted=False, acquired=False)),
        ),
        patch("ontokit.api.routes.translation.get_arq_pool", AsyncMock(return_value=pool)),
        pytest.raises(HTTPException) as limited,
    ):
        await translate_entity_field(project_id, request, db, user)
    assert limited.value.status_code == 429

    reserve_units = AsyncMock(
        return_value=RateLimitReservation(accepted=True, acquired=True)
    )
    with (
        patch("ontokit.api.routes.translation._require_member", AsyncMock(return_value="editor")),
        patch(
            "ontokit.api.routes.translation._get_config",
            AsyncMock(return_value=_config(language_tags=["fr"])),
        ),
        patch("ontokit.api.routes.translation._get_redis", return_value=AsyncMock()),
        patch("ontokit.api.routes.translation.reserve_rate_limit_units", reserve_units),
        patch("ontokit.api.routes.translation.get_arq_pool", AsyncMock(return_value=None)),
        pytest.raises(HTTPException) as unavailable,
    ):
        await translate_entity_field(project_id, request, db, user)
    assert unavailable.value.status_code == 503
    reserve_units.assert_not_awaited()

    redis = AsyncMock()
    redis.incrby.return_value = 1
    reserve_units = AsyncMock(
        return_value=RateLimitReservation(accepted=True, acquired=True)
    )
    with (
        patch("ontokit.api.routes.translation._require_member", AsyncMock(return_value="editor")),
        patch(
            "ontokit.api.routes.translation._get_config",
            AsyncMock(
                return_value=_config(language_tags=["fr"], verification_mechanism="confidence")
            ),
        ),
        patch("ontokit.api.routes.translation._get_redis", return_value=redis),
        patch(
            "ontokit.api.routes.translation.reserve_rate_limit_units",
            reserve_units,
        ),
        patch("ontokit.api.routes.translation.get_arq_pool", AsyncMock(return_value=pool)),
    ):
        response = await translate_entity_field(project_id, request, db, user)
    assert response.job_id == "translation-1"
    consume_call = reserve_units.await_args
    assert consume_call.args == (redis, str(project_id), "actor-1", "editor", 2)
    assert consume_call.kwargs["reservation_id"]
    pool.enqueue_job.assert_awaited_once_with(
        "run_translation_entity_task",
        str(project_id),
        "main",
        str(EX.cat),
        str(SKOS.definition),
        None,
        None,
        None,
        "actor-1",
        "fast",
        _job_id=ANY,
    )


@pytest.mark.asyncio
async def test_on_demand_duplicate_and_enqueue_failure_reuse_idempotent_reservation() -> None:
    project_id = uuid4()
    request = TranslateFieldRequest(
        entity_iri=str(EX.cat), predicate="skos:definition", branch="main"
    )
    db = AsyncMock()
    user = CurrentUser(id="actor-1", name="Actor", email="actor@example.test")
    redis = AsyncMock()
    redis.incrby.return_value = 1
    reserve_units = AsyncMock(
        side_effect=[
            RateLimitReservation(accepted=True, acquired=False),
            RateLimitReservation(accepted=True, acquired=True),
        ]
    )
    release_units = AsyncMock(return_value=True)
    pool = AsyncMock()
    pool.enqueue_job.return_value = None

    with (
        patch("ontokit.api.routes.translation._require_member", AsyncMock(return_value="editor")),
        patch(
            "ontokit.api.routes.translation._get_config",
            AsyncMock(
                return_value=_config(language_tags=["fr"], verification_mechanism="confidence")
            ),
        ),
        patch("ontokit.api.routes.translation._get_redis", return_value=redis),
        patch("ontokit.api.routes.translation.reserve_rate_limit_units", reserve_units),
        patch("ontokit.api.routes.translation.release_rate_limit_units", release_units),
        patch("ontokit.api.routes.translation.get_arq_pool", AsyncMock(return_value=pool)),
    ):
        duplicate = await translate_entity_field(project_id, request, db, user)

    assert duplicate.job_id.startswith("translation-entity:")
    redis.decrby.assert_awaited_once()
    release_units.assert_not_awaited()

    pool.enqueue_job.side_effect = ConnectionError("queue down")
    with (
        patch("ontokit.api.routes.translation._require_member", AsyncMock(return_value="editor")),
        patch(
            "ontokit.api.routes.translation._get_config",
            AsyncMock(
                return_value=_config(language_tags=["fr"], verification_mechanism="confidence")
            ),
        ),
        patch("ontokit.api.routes.translation._get_redis", return_value=redis),
        patch("ontokit.api.routes.translation.reserve_rate_limit_units", reserve_units),
        patch("ontokit.api.routes.translation.release_rate_limit_units", release_units),
        patch("ontokit.api.routes.translation.get_arq_pool", AsyncMock(return_value=pool)),
        pytest.raises(HTTPException) as refused,
    ):
        await translate_entity_field(project_id, request, db, user)

    assert refused.value.status_code == 503
    assert redis.decrby.await_count == 2
    reservation_ids = [call.kwargs["reservation_id"] for call in reserve_units.await_args_list]
    assert reservation_ids == [duplicate.job_id, duplicate.job_id]
    release_units.assert_awaited_once_with(
        redis,
        str(project_id),
        "actor-1",
        "editor",
        2,
        reservation_id=duplicate.job_id,
    )
