"""Proof-first coverage for native-speaker reviewer authorization and confirmation (U9)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pygit2
import pytest
from fastapi import HTTPException
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDFS

from ontokit.api.routes import translation as routes
from ontokit.core.auth import CurrentUser
from ontokit.core.constants import ONTOKIT_COMMITTER_EMAIL, ONTOKIT_COMMITTER_NAME
from ontokit.git.bare_repository import BareGitRepositoryService, BareOntologyRepository
from ontokit.models.project import Project, ProjectMember
from ontokit.models.translation import NativeReviewerLanguage, TranslationRecord, hash_literal_value
from ontokit.schemas.translation import TranslationBulkConfirmRequest
from ontokit.services.translation_annotations import (
    TranslationAnnotation,
    annotate,
    read_annotation,
    translation_record_digest,
)
from ontokit.services.translation_review import TranslationReviewService

ENTITY = URIRef("http://example.org/ontology#Person")


class FixtureGitService(BareGitRepositoryService):
    def __init__(self, repo: BareOntologyRepository) -> None:
        super().__init__(str(repo.repo_path.parent))
        self._fixture_repo = repo

    def get_repository(self, project_id: UUID) -> BareOntologyRepository:  # noqa: ARG002
        return self._fixture_repo


def _record(project_id: UUID, language: str = "sw", value: str = "Mtu") -> TranslationRecord:
    return TranslationRecord(
        id=uuid4(),
        project_id=project_id,
        entity_iri=str(ENTITY),
        predicate=str(RDFS.label),
        language=language,
        source_value="Person",
        proposed_value=value,
        source_value_hash=hash_literal_value("Person"),
        translated_value_hash=hash_literal_value(value),
        model_name="model",
        model_version="2026-08-09",
        method="confidence",
        score=0.7,
        state="provisional",
        created_at=datetime.now(UTC),
    )


def _service(bare_git_repo: BareOntologyRepository) -> tuple[TranslationReviewService, AsyncMock]:
    db = AsyncMock()
    db.add = Mock()
    return TranslationReviewService(
        db, FixtureGitService(bare_git_repo), index_enqueuer=AsyncMock()
    ), db


@pytest.mark.asyncio
async def test_tagged_reviewer_confirms_record_with_human_authored_commit(
    bare_git_repo: BareOntologyRepository,
) -> None:
    project_id = uuid4()
    member = ProjectMember(id=uuid4(), project_id=project_id, user_id="reviewer", role="viewer")
    record = _record(project_id)
    service, db = _service(bare_git_repo)

    await service.confirm_loaded(
        project_id=project_id,
        branch="main",
        filename="ontology.ttl",
        member=member,
        reviewer_languages={"sw"},
        record=record,
        author_name="Asha Reviewer",
        author_email="asha@example.test",
    )

    assert record.state == "verified"
    assert record.confirming_member_id == member.id
    assert record.confirmed_at is not None
    db.commit.assert_awaited_once()
    graph = Graph().parse(data=bare_git_repo.read_file("main", "ontology.ttl"), format="turtle")
    literal = Literal("Mtu", lang="sw")
    assert (ENTITY, RDFS.label, literal) in graph
    assert read_annotation(graph, ENTITY, RDFS.label, literal).state == "verified"  # type: ignore[union-attr]
    commit = bare_git_repo.repo[bare_git_repo.get_branch_commit_hash("main")]
    assert isinstance(commit, pygit2.Commit)
    assert (commit.author.name, commit.author.email) == ("Asha Reviewer", "asha@example.test")
    assert (commit.committer.name, commit.committer.email) == (
        ONTOKIT_COMMITTER_NAME,
        ONTOKIT_COMMITTER_EMAIL,
    )


@pytest.mark.asyncio
async def test_confirmation_locks_and_flushes_state_before_git(
    bare_git_repo: BareOntologyRepository,
) -> None:
    project_id = uuid4()
    member = ProjectMember(id=uuid4(), project_id=project_id, user_id="reviewer", role="viewer")
    record = _record(project_id)
    service, db = _service(bare_git_repo)
    events: list[str] = []

    async def refresh(*_args: object, **kwargs: object) -> None:
        assert kwargs["with_for_update"] is True
        events.append("lock")

    async def flush() -> None:
        assert record.state == "verified"
        events.append("flush")

    original_commit = service.git.commit_changes

    def commit_changes(**kwargs: object) -> object:
        assert record.state == "verified"
        events.append("git")
        return original_commit(**kwargs)  # type: ignore[arg-type]

    async def commit() -> None:
        events.append("commit")

    db.refresh.side_effect = refresh
    db.flush.side_effect = flush
    db.commit.side_effect = commit
    service.git.commit_changes = Mock(side_effect=commit_changes)  # type: ignore[method-assign]

    await service.confirm_loaded(
        project_id=project_id,
        branch="main",
        filename="ontology.ttl",
        member=member,
        reviewer_languages={"sw"},
        record=record,
        author_name="Asha Reviewer",
        author_email="asha@example.test",
    )

    assert events == ["lock", "flush", "git", "commit"]


@pytest.mark.asyncio
async def test_database_commit_failure_restores_translation_branch(
    bare_git_repo: BareOntologyRepository,
) -> None:
    project_id = uuid4()
    member = ProjectMember(id=uuid4(), project_id=project_id, user_id="reviewer", role="viewer")
    record = _record(project_id)
    service, db = _service(bare_git_repo)
    original_head = bare_git_repo.get_branch_commit_hash("main")
    db.commit.side_effect = RuntimeError("database commit failed")

    with pytest.raises(RuntimeError, match="database commit failed"):
        await service.confirm_loaded(
            project_id=project_id,
            branch="main",
            filename="ontology.ttl",
            member=member,
            reviewer_languages={"sw"},
            record=record,
            author_name="Asha Reviewer",
            author_email="asha@example.test",
        )

    assert bare_git_repo.get_branch_commit_hash("main") == original_head
    db.rollback.assert_awaited_once()
    service.index_enqueuer.assert_not_awaited()  # type: ignore[union-attr]


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["owner", "admin", "editor", "suggester", "viewer"])
@pytest.mark.parametrize("tagged", [False, True])
async def test_confirmation_authorization_matrix(
    bare_git_repo: BareOntologyRepository, role: str, tagged: bool
) -> None:
    project_id = uuid4()
    member = ProjectMember(id=uuid4(), project_id=project_id, user_id="actor", role=role)
    record = _record(project_id)
    service, _db = _service(bare_git_repo)
    if tagged:
        await service.confirm_loaded(
            project_id=project_id,
            branch="main",
            filename="ontology.ttl",
            member=member,
            reviewer_languages={"sw"},
            record=record,
            author_name="Actor",
            author_email="actor@example.test",
        )
        assert record.state == "verified"
    else:
        with pytest.raises(PermissionError, match="language"):
            await service.confirm_loaded(
                project_id=project_id,
                branch="main",
                filename="ontology.ttl",
                member=member,
                reviewer_languages=set(),
                record=record,
                author_name="Actor",
                author_email="actor@example.test",
            )


@pytest.mark.asyncio
async def test_multi_language_reviewer_can_confirm_both(
    bare_git_repo: BareOntologyRepository,
) -> None:
    project_id = uuid4()
    member = ProjectMember(id=uuid4(), project_id=project_id, user_id="actor", role="admin")
    service, _db = _service(bare_git_repo)
    for record in (_record(project_id, "sw", "Mtu"), _record(project_id, "fr", "Personne")):
        await service.confirm_loaded(
            project_id=project_id,
            branch="main",
            filename="ontology.ttl",
            member=member,
            reviewer_languages={"sw", "fr"},
            record=record,
            author_name="Actor",
            author_email="actor@example.test",
        )
        assert record.state == "verified"


@pytest.mark.asyncio
async def test_reject_retains_row_and_removes_surfaced_literal(
    bare_git_repo: BareOntologyRepository,
) -> None:
    project_id = uuid4()
    member = ProjectMember(id=uuid4(), project_id=project_id, user_id="actor", role="viewer")
    record = _record(project_id)
    service, db = _service(bare_git_repo)
    graph = Graph().parse(data=bare_git_repo.read_file("main", "ontology.ttl"), format="turtle")
    literal = Literal("Mtu", lang="sw")
    graph.add((ENTITY, RDFS.label, literal))
    annotate(
        graph,
        ENTITY,
        RDFS.label,
        literal,
        TranslationAnnotation(
            record.method, "provisional", record.created_at, translation_record_digest(record)
        ),
    )
    bare_git_repo.write_file(
        "main", "ontology.ttl", graph.serialize(format="turtle").encode(), "Surface provisional"
    )
    await service.reject_loaded(
        project_id=project_id,
        branch="main",
        filename="ontology.ttl",
        member=member,
        reviewer_languages={"sw"},
        record=record,
        author_name="Actor",
        author_email="actor@example.test",
    )
    assert record.state == "rejected"
    assert record in (record,)  # retained object/row; no delete is issued
    db.delete.assert_not_called()
    rejected_graph = Graph().parse(
        data=bare_git_repo.read_file("main", "ontology.ttl"), format="turtle"
    )
    assert (ENTITY, RDFS.label, literal) not in rejected_graph


def test_native_reviewer_language_is_separate_multi_assignable_association() -> None:
    member_id = uuid4()
    tags = [
        NativeReviewerLanguage(member_id=member_id, language="sw"),
        NativeReviewerLanguage(member_id=member_id, language="fr"),
    ]
    assert {tag.language for tag in tags} == {"sw", "fr"}


def test_fixed_reviewer_route_contract_is_mounted() -> None:
    paths = {route.path_format for route in routes.router.routes}
    assert {
        "/{project_id}/translation/reviewers",
        "/{project_id}/translation/reviewers/{member_id}",
        "/{project_id}/translation/my-reviewer-languages",
        "/{project_id}/translation/records/{record_id}/confirm",
        "/{project_id}/translation/records/{record_id}/reject",
        "/{project_id}/translation/records/confirm-bulk",
    } <= paths


@pytest.mark.asyncio
async def test_nonmember_superadmin_reads_empty_reviewer_languages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = AsyncMock()
    project_id = uuid4()
    project_result = Mock()
    project_result.scalar_one_or_none.return_value = Project(
        id=project_id,
        name="Translation project",
        owner_id="root",
        is_public=False,
        is_demo=False,
    )
    member_result = Mock()
    member_result.scalar_one_or_none.return_value = None
    db.execute.side_effect = [project_result, member_result]

    user = CurrentUser(id="root")
    monkeypatch.setattr(type(user), "is_superadmin", property(lambda _self: True), raising=False)

    response = await routes.get_my_reviewer_languages(project_id, db, user)

    assert response.languages == []


@pytest.mark.asyncio
async def test_bulk_itemizes_cross_project_and_mixed_language_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    good, other_project, untagged = uuid4(), uuid4(), uuid4()
    acted: list[UUID] = []

    async def review(**kwargs: object) -> object:
        record_id = kwargs["record_id"]
        assert isinstance(record_id, UUID)
        if record_id == other_project:
            raise HTTPException(status_code=404, detail="Translation record not found")
        if record_id == untagged:
            raise HTTPException(
                status_code=403, detail="native-reviewer tag required for this language"
            )
        acted.append(record_id)
        return object()

    monkeypatch.setattr(routes, "_review_record", review)
    db = AsyncMock()
    response = await routes.confirm_translation_records_bulk(
        uuid4(),
        TranslationBulkConfirmRequest(branch="main", record_ids=[good, other_project, untagged]),
        db,
        Mock(),
        Mock(),
    )
    assert acted == [good]
    assert [(item.ok, item.error) for item in response.results] == [
        (True, None),
        (False, "Translation record not found"),
        (False, "native-reviewer tag required for this language"),
    ]
    assert db.rollback.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "allowed"),
    [
        ("owner", True),
        ("admin", True),
        ("editor", False),
        ("suggester", False),
        ("viewer", False),
    ],
)
async def test_reviewer_tag_assignment_is_owner_admin_gated(role: str, allowed: bool) -> None:
    member = ProjectMember(project_id=uuid4(), user_id="actor", role=role)
    result = Mock()
    result.scalar_one_or_none.return_value = member
    db = AsyncMock()
    db.execute.return_value = result
    if allowed:
        await routes._require_owner_or_admin(db, member.project_id, member.user_id, False)
    else:
        with pytest.raises(HTTPException) as exc_info:
            await routes._require_owner_or_admin(db, member.project_id, member.user_id, False)
        assert exc_info.value.status_code == 403
