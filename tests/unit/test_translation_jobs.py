"""Proof-first coverage for mint-triggered translation jobs (U6)."""

from __future__ import annotations

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
from ontokit.services.translation_jobs import (
    MAX_PENDING_TRANSLATIONS_PER_PROJECT,
    TranslationTask,
    discover_translation_tasks,
    enqueue_label_diff_after_commit,
    enqueue_translation_tasks,
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


def test_definition_scope_off_is_skipped() -> None:
    current = _graph((EX.cat, SKOS.definition, Literal("A small mammal", lang="en")))
    assert discover_translation_tasks(Graph(), current, _config(), covered=set()) == []


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
async def test_mint_rate_limit_is_attributed_per_actor() -> None:
    pool = AsyncMock()
    pool.enqueue_job.return_value = Mock(job_id="diff-1")

    async def actor_limit(_redis: object, _project: str, actor: str, _role: str) -> bool:
        return actor != "churning-actor"

    with (
        patch("ontokit.api.utils.redis.get_arq_pool", AsyncMock(return_value=pool)),
        patch("ontokit.main.redis_pool", pool),
        patch("ontokit.services.translation_jobs.check_rate_limit", side_effect=actor_limit),
    ):
        refused = await enqueue_label_diff_after_commit(
            project_id=uuid4(),
            branch="main",
            commit_hash="abc",
            actor_id="churning-actor",
            role="editor",
        )
        accepted = await enqueue_label_diff_after_commit(
            project_id=uuid4(),
            branch="main",
            commit_hash="def",
            actor_id="other-actor",
            role="editor",
        )

    assert refused is False
    assert accepted is True
    pool.enqueue_job.assert_awaited_once()


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
            AsyncMock(return_value=_config(language_tags=["fr"], verification_mechanism="confidence")),
        ),
        patch("ontokit.api.routes.translation._get_redis", return_value=AsyncMock()),
        patch(
            "ontokit.api.routes.translation.get_remaining_calls",
            AsyncMock(return_value=100),
        ),
        patch("ontokit.api.routes.translation.check_rate_limit", AsyncMock(return_value=False)),
        pytest.raises(HTTPException) as limited,
    ):
        await translate_entity_field(project_id, request, db, user)
    assert limited.value.status_code == 429

    redis = AsyncMock()
    redis.incrby.return_value = 1
    with (
        patch(
            "ontokit.api.routes.translation._require_member", AsyncMock(return_value="editor")
        ),
        patch(
            "ontokit.api.routes.translation._get_config",
            AsyncMock(return_value=_config(language_tags=["fr"], verification_mechanism="confidence")),
        ),
        patch("ontokit.api.routes.translation._get_redis", return_value=redis),
        patch(
            "ontokit.api.routes.translation.get_remaining_calls",
            AsyncMock(return_value=100),
        ),
        patch("ontokit.api.routes.translation.check_rate_limit", AsyncMock(return_value=True)),
        patch("ontokit.api.routes.translation.get_arq_pool", AsyncMock(return_value=pool)),
    ):
        response = await translate_entity_field(project_id, request, db, user)
    assert response.job_id == "translation-1"
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
