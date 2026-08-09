"""Cross-layer proofs for the translation lifecycle (U14)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pygit2
import pytest
from fastapi import HTTPException
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import SKOS
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.api.routes import projects, translation
from ontokit.core.auth import CurrentUser
from ontokit.git.bare_repository import BareGitRepositoryService
from ontokit.models.llm_config import LLMAuditLog, ProjectLLMConfig
from ontokit.models.project import Project, ProjectMember
from ontokit.models.translation import (
    NativeReviewerLanguage,
    TranslationRecord,
    hash_literal_value,
)
from ontokit.schemas.project import SourceContentSave
from ontokit.schemas.translation import (
    TranslationBackfillRequest,
    TranslationConfigUpdate,
    TranslationReviewRequest,
)
from ontokit.services.ontology_index import OntologyIndexService
from ontokit.services.project_service import ProjectService
from ontokit.services.translation_annotations import read_annotation
from ontokit.services.translation_backfill import select_backfill_literals
from ontokit.services.translation_jobs import (
    run_label_diff_job,
    run_translation_backfill_job,
    run_translation_entity_job,
)

pytestmark = pytest.mark.integration

EX = "https://example.test/"
FILE = "ontology.ttl"
BASE = """\
@prefix ex: <https://example.test/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
"""


class InlineQueue:
    """Record ARQ payloads while delegating Redis counter operations to real Redis."""

    def __init__(self, redis: object) -> None:
        self.redis = redis
        self.jobs: list[tuple[str, tuple[object, ...]]] = []

    async def enqueue_job(
        self, name: str, *args: object, **kwargs: object
    ) -> SimpleNamespace:
        self.jobs.append((name, args))
        return SimpleNamespace(job_id=kwargs.get("_job_id", f"inline-{len(self.jobs)}"))

    def __getattr__(self, name: str) -> object:
        return getattr(self.redis, name)


class RouteOntology:
    """Minimal ontology collaborator; Git and parsing remain real."""

    def __init__(self, git: BareGitRepositoryService) -> None:
        self.git = git
        self.graphs: dict[tuple[UUID, str], Graph] = {}

    def is_loaded(self, project_id: UUID, branch: str) -> bool:
        return (project_id, branch) in self.graphs

    async def load_from_git(
        self, project_id: UUID, branch: str, filename: str, git: BareGitRepositoryService
    ) -> Graph:
        graph = Graph().parse(
            data=git.get_file_from_branch(project_id, branch, filename), format="turtle"
        )
        self.graphs[(project_id, branch)] = graph
        return graph

    async def _get_graph(self, project_id: UUID, branch: str) -> Graph:
        return self.graphs[(project_id, branch)]

    def unload(self, project_id: UUID, branch: str) -> None:
        self.graphs.pop((project_id, branch), None)


async def _seed(
    db: AsyncSession, tmp_path: Path, *, languages: list[str], provisional_gate: bool = False
) -> tuple[UUID, CurrentUser, CurrentUser, CurrentUser, BareGitRepositoryService]:
    project_id = uuid4()
    owner = CurrentUser(id=f"owner-{project_id}", name="Owner", email="owner@example.test")
    reviewer = CurrentUser(
        id=f"reviewer-{project_id}", name="Asha Reviewer", email="asha@example.test"
    )
    admin = CurrentUser(id=f"admin-{project_id}", name="Admin", email="admin@example.test")
    project = Project(
        id=project_id,
        name="Translation integration",
        owner_id=owner.id,
        source_file_path=FILE,
    )
    owner_member = ProjectMember(project_id=project_id, user_id=owner.id, role="owner")
    reviewer_member = ProjectMember(project_id=project_id, user_id=reviewer.id, role="viewer")
    admin_member = ProjectMember(project_id=project_id, user_id=admin.id, role="admin")
    db.add_all(
        [
            project,
            owner_member,
            reviewer_member,
            admin_member,
            ProjectLLMConfig(project_id=project_id, provider="openai", model="gpt-4o-mini"),
        ]
    )
    await db.commit()
    db.add(NativeReviewerLanguage(member_id=reviewer_member.id, language="sw"))
    await db.commit()
    await translation.update_translation_config(
        project_id,
        TranslationConfigUpdate(
            language_tags=languages,
            verification_mechanism="consensus",
            consensus_threshold=0.85,
            speed_mode="fast",
            provisional_gate=provisional_gate,
            primary_provider="openai",
            primary_model="gpt-4o-mini",
        ),
        db,
        owner,
    )
    git = BareGitRepositoryService(base_path=str(tmp_path))
    git.initialize_repository(project_id, BASE.encode(), FILE)
    return project_id, owner, reviewer, admin, git


async def _cleanup(db: AsyncSession, project_id: UUID) -> None:
    await db.rollback()
    await db.execute(delete(Project).where(Project.id == project_id))
    await db.commit()


def _fake_chat(messages: list[dict[str, str]], **_kwargs: object) -> tuple[str, int, int]:
    system = messages[0]["content"]
    user_content = messages[-1]["content"]
    payload = json.loads(user_content.split("\n", 1)[1].rsplit("\n", 1)[0])
    if "Judge semantic agreement" in system:
        agreement = (
            0.0
            if payload["target_language"] == "de" and payload["source_literal"] != "Agreement"
            else 1.0
        )
        return json.dumps({"agreement": agreement}), 12, 4
    if "Back-translate" in system:
        value = payload["translated_literal"]
        reverse = {
            "Dépôt": "Bailment",
            "Cautionnement": "Security interest",
            "Amana": "Bailment",
            "Dhamana": "Security interest",
            "Mkataba": "Agreement",
            "Accord": "Agreement",
            "Vereinbarung": "Agreement",
        }
        return json.dumps({"translation": reverse.get(value, "Unrelated")}), 10, 4
    source, target = payload["source_literal"], payload["target_language"]
    values = {
        ("Bailment", "fr"): "Dépôt",
        ("Security interest", "fr"): "Cautionnement",
        ("Bailment", "de"): "Verwahrung",
        ("Security interest", "de"): "Sicherheit",
        ("Bailment", "sw"): "Amana",
        ("Security interest", "sw"): "Dhamana",
        ("Agreement", "fr"): "Accord",
        ("Agreement", "de"): "Vereinbarung",
        ("Agreement", "sw"): "Mkataba",
    }
    return json.dumps({"translation": values[(source, target)]}), 14, 5


async def _source_save(
    db: AsyncSession,
    git: BareGitRepositoryService,
    queue: InlineQueue,
    project_id: UUID,
    user: CurrentUser,
    content: str,
) -> str:
    ontology = RouteOntology(git)
    await ontology.load_from_git(project_id, "main", FILE, git)
    with (
        patch("ontokit.api.routes.projects.get_arq_pool", AsyncMock(return_value=queue)),
        patch("ontokit.api.utils.redis.get_arq_pool", AsyncMock(return_value=queue)),
        patch("ontokit.main.redis_pool", queue),
        patch("ontokit.services.translation_jobs.check_rate_limit", AsyncMock(return_value=True)),
    ):
        result = await projects.save_source_content(
            project_id,
            SourceContentSave(content=content, commit_message="Mint translated concept"),
            db,
            ProjectService(db, git),
            SimpleNamespace(upload_file=AsyncMock()),
            ontology,
            git,
            SimpleNamespace(record_events_from_diff=AsyncMock(return_value=[])),
            SimpleNamespace(),
            user,
            branch="main",
        )
    return result.commit_hash


async def _run_mint_jobs(
    db: AsyncSession,
    queue: InlineQueue,
    git: BareGitRepositoryService,
    project_id: UUID,
    commit_hash: str,
    actor_id: str,
) -> list[dict[str, object]]:
    ctx = {"db": db, "redis": queue}
    with (
        patch("ontokit.services.translation_jobs.get_git_service", return_value=git),
        patch(
            "ontokit.services.llm.openai_compat.OpenAICompatProvider.chat",
            new=AsyncMock(side_effect=_fake_chat),
        ),
    ):
        await run_label_diff_job(ctx, str(project_id), "main", commit_hash, actor_id)
        entity_jobs = [args for name, args in queue.jobs if name == "run_translation_entity_task"]
        return [await run_translation_entity_job(ctx, *args) for args in entity_jobs]


async def _index(db: AsyncSession, git: BareGitRepositoryService, project_id: UUID) -> None:
    graph = Graph().parse(data=git.get_file_from_branch(project_id, "main", FILE), format="turtle")
    await OntologyIndexService(db).full_reindex(
        project_id, "main", graph, git.get_repository(project_id).get_branch_commit_hash("main")
    )


@pytest.mark.asyncio
async def test_full_mint_loop_commits_verified_labels_and_reports_coverage(
    real_db_session: AsyncSession, real_redis: object, tmp_path: Path
) -> None:
    project_id, owner, _reviewer, _admin, git = await _seed(
        real_db_session, tmp_path, languages=["fr", "de"]
    )
    queue = InlineQueue(real_redis)
    entity = URIRef(f"{EX}bailment")
    content = (
        BASE
        + 'ex:bailment a owl:Class ; skos:prefLabel "Bailment"@en ; skos:altLabel "Security interest"@en .\n'
    )
    try:
        commit_hash = await _source_save(real_db_session, git, queue, project_id, owner, content)
        results = await _run_mint_jobs(
            real_db_session, queue, git, project_id, commit_hash, owner.id
        )
        assert len(results) == 4
        graph = Graph().parse(
            data=git.get_file_from_branch(project_id, "main", FILE), format="turtle"
        )
        for predicate, value in ((SKOS.prefLabel, "Dépôt"), (SKOS.altLabel, "Cautionnement")):
            literal = Literal(value, lang="fr")
            assert (entity, predicate, literal) in graph
            annotation = read_annotation(graph, entity, predicate, literal)
            assert annotation is not None and annotation.state == "verified"
        assert not any(
            literal.language == "de" for literal in graph.objects(entity, SKOS.prefLabel)
        )
        records = list(
            (
                await real_db_session.scalars(
                    select(TranslationRecord).where(TranslationRecord.project_id == project_id)
                )
            ).all()
        )
        assert {(record.language, record.state) for record in records} == {
            ("fr", "verified"),
            ("de", "provisional"),
        }
        assert (
            await real_db_session.scalar(
                select(func.count())
                .select_from(LLMAuditLog)
                .where(LLMAuditLog.project_id == project_id)
            )
            == 16
        )
        head = git.get_repository(project_id).repo[
            git.get_repository(project_id).get_branch_commit_hash("main")
        ]
        assert isinstance(head, pygit2.Commit)
        assert (head.author.name, head.committer.name) == ("translation-bot", "OntoKit-bot")
        await _index(real_db_session, git, project_id)
        coverage = await translation.get_translation_coverage(
            project_id, "main", real_db_session, owner, git
        )
        rows = {row["language"]: row for row in coverage["languages"]}  # type: ignore[index]
        assert rows["fr"]["verified"] == 2
        assert rows["de"]["provisional"] == 2
    finally:
        await _cleanup(real_db_session, project_id)


@pytest.mark.asyncio
async def test_provisional_gate_requires_tagged_confirmation_and_human_authors_commit(
    real_db_session: AsyncSession, real_redis: object, tmp_path: Path
) -> None:
    project_id, owner, reviewer, admin, git = await _seed(
        real_db_session, tmp_path, languages=["sw"], provisional_gate=True
    )
    queue = InlineQueue(real_redis)
    content = BASE + 'ex:bailment a owl:Class ; skos:prefLabel "Bailment"@en .\n'
    try:
        commit_hash = await _source_save(real_db_session, git, queue, project_id, owner, content)
        before = git.get_repository(project_id).get_branch_commit_hash("main")
        await _run_mint_jobs(real_db_session, queue, git, project_id, commit_hash, owner.id)
        record = await real_db_session.scalar(
            select(TranslationRecord).where(TranslationRecord.project_id == project_id)
        )
        assert record is not None and record.state == "provisional"
        assert git.get_repository(project_id).get_branch_commit_hash("main") == before
        with pytest.raises(HTTPException) as forbidden:
            await translation.confirm_translation_record(
                project_id,
                record.id,
                TranslationReviewRequest(branch="main"),
                real_db_session,
                admin,
                git,
            )
        assert forbidden.value.status_code == 403
        confirmed = await translation.confirm_translation_record(
            project_id,
            record.id,
            TranslationReviewRequest(branch="main"),
            real_db_session,
            reviewer,
            git,
        )
        assert confirmed.state == "verified"
        head = git.get_repository(project_id).repo[
            git.get_repository(project_id).get_branch_commit_hash("main")
        ]
        assert isinstance(head, pygit2.Commit)
        assert head.author.name == "Asha Reviewer"
        graph = Graph().parse(
            data=git.get_file_from_branch(project_id, "main", FILE), format="turtle"
        )
        literal = Literal("Amana", lang="sw")
        assert (URIRef(f"{EX}bailment"), SKOS.prefLabel, literal) in graph
        assert read_annotation(graph, URIRef(f"{EX}bailment"), SKOS.prefLabel, literal) is not None
    finally:
        await _cleanup(real_db_session, project_id)


@pytest.mark.asyncio
async def test_cost_preview_spends_nothing_then_backfill_fills_added_language(
    real_db_session: AsyncSession, real_redis: object, tmp_path: Path
) -> None:
    project_id, owner, _reviewer, _admin, git = await _seed(
        real_db_session, tmp_path, languages=["fr"]
    )
    content = BASE + 'ex:agreement a owl:Class ; skos:prefLabel "Agreement"@en .\n'
    git.commit_changes(project_id, content.encode(), FILE, "Seed", owner.name, owner.email, "main")
    try:
        await _index(real_db_session, git, project_id)
        await translation.update_translation_config(
            project_id,
            TranslationConfigUpdate(language_tags=["fr", "de"]),
            real_db_session,
            owner,
        )
        before = await real_db_session.scalar(
            select(func.count())
            .select_from(LLMAuditLog)
            .where(LLMAuditLog.project_id == project_id)
        )
        preview = await translation.preview_translation_backfill(
            project_id, "main", real_db_session, owner, git, language="de"
        )
        assert preview.literal_count == 1 and preview.expected_cost_usd > 0
        assert (
            await real_db_session.scalar(
                select(func.count())
                .select_from(LLMAuditLog)
                .where(LLMAuditLog.project_id == project_id)
            )
            == before
        )
        queue = InlineQueue(real_redis)
        with patch("ontokit.api.routes.translation.get_arq_pool", AsyncMock(return_value=queue)):
            accepted = await translation.launch_translation_backfill(
                project_id,
                TranslationBackfillRequest(branch="main", language="de"),
                real_db_session,
                owner,
            )
        with (
            patch("ontokit.services.translation_jobs.get_git_service", return_value=git),
            patch(
                "ontokit.services.llm.openai_compat.OpenAICompatProvider.chat",
                new=AsyncMock(side_effect=_fake_chat),
            ),
        ):
            result = await run_translation_backfill_job(
                {"db": real_db_session, "redis": queue},
                str(project_id),
                "main",
                accepted.job_id,
                owner.id,
            )
        assert result["completed"] == 1
        await _index(real_db_session, git, project_id)
        coverage = await translation.get_translation_coverage(
            project_id, "main", real_db_session, owner, git
        )
        de = next(row for row in coverage["languages"] if row["language"] == "de")  # type: ignore[index]
        assert de["verified"] == 1 and de["missing"] == 0
    finally:
        await _cleanup(real_db_session, project_id)


@pytest.mark.asyncio
async def test_era_scope_selects_only_old_never_confirmed_records(
    real_db_session: AsyncSession, tmp_path: Path
) -> None:
    project_id, _owner, reviewer, _admin, git = await _seed(
        real_db_session, tmp_path, languages=["fr"]
    )
    cutoff = datetime.now(UTC) - timedelta(days=30)
    old = cutoff - timedelta(days=30)
    member = await real_db_session.scalar(
        select(ProjectMember).where(ProjectMember.user_id == reviewer.id)
    )
    records = [
        TranslationRecord(
            project_id=project_id,
            entity_iri=f"{EX}{name}",
            predicate=str(SKOS.prefLabel),
            language="fr",
            source_value=name.title(),
            proposed_value=value,
            source_value_hash=hash_literal_value(name.title()),
            translated_value_hash=hash_literal_value(value),
            model_name="old-model",
            model_version="2026",
            method="consensus",
            score=0.9,
            state="verified",
            created_at=created,
            confirmed_at=confirmed,
            confirming_member_id=member.id if confirmed and member else None,
        )
        for name, value, created, confirmed in (
            ("cat", "Chat", old, None),
            ("dog", "Chien", old, old),
            ("bird", "Oiseau", datetime.now(UTC), None),
        )
    ]
    git.commit_changes(
        project_id,
        (
            BASE
            + 'ex:cat skos:prefLabel "Cat"@en .\n'
            + 'ex:dog skos:prefLabel "Dog"@en .\n'
            + 'ex:bird skos:prefLabel "Bird"@en .\n'
        ).encode(),
        FILE,
        "Seed era sources",
        branch_name="main",
    )
    real_db_session.add_all(records)
    await real_db_session.commit()
    try:
        selected = await select_backfill_literals(
            real_db_session,
            git,
            project_id,
            "main",
            era_before=cutoff,
            never_confirmed=True,
        )
        assert [item.entity_iri for item in selected] == [f"{EX}cat"]
        await real_db_session.refresh(records[1])
        assert records[1].confirmed_at == old
    finally:
        await _cleanup(real_db_session, project_id)


@pytest.mark.asyncio
async def test_in_flight_source_edit_discards_stale_result_without_translation_commit(
    real_db_session: AsyncSession, real_redis: object, tmp_path: Path
) -> None:
    project_id, owner, _reviewer, _admin, git = await _seed(
        real_db_session, tmp_path, languages=["fr"]
    )
    queue = InlineQueue(real_redis)
    minted = BASE + 'ex:agreement a owl:Class ; skos:prefLabel "Agreement"@en .\n'
    edited = BASE + 'ex:agreement a owl:Class ; skos:prefLabel "Contract"@en .\n'
    try:
        mint_hash = await _source_save(real_db_session, git, queue, project_id, owner, minted)
        with patch("ontokit.services.translation_jobs.get_git_service", return_value=git):
            await run_label_diff_job(
                {"db": real_db_session, "redis": queue},
                str(project_id),
                "main",
                mint_hash,
                owner.id,
            )
        task = next(args for name, args in queue.jobs if name == "run_translation_entity_task")
        await _source_save(real_db_session, git, InlineQueue(real_redis), project_id, owner, edited)
        edited_head = git.get_repository(project_id).get_branch_commit_hash("main")
        with (
            patch("ontokit.services.translation_jobs.get_git_service", return_value=git),
            patch(
                "ontokit.services.llm.openai_compat.OpenAICompatProvider.chat",
                new=AsyncMock(side_effect=_fake_chat),
            ),
        ):
            outcome = await run_translation_entity_job(
                {"db": real_db_session, "redis": queue}, *task
            )
        assert outcome["commit_hash"] is None
        assert git.get_repository(project_id).get_branch_commit_hash("main") == edited_head
        record = await real_db_session.scalar(
            select(TranslationRecord).where(TranslationRecord.project_id == project_id)
        )
        assert record is not None and record.state == "rejected"
    finally:
        await _cleanup(real_db_session, project_id)
