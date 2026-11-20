"""Exercise history reconciliation with real Git, RDF and database rows."""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pygit2
import pytest
import sqlalchemy as sa
from rdflib.namespace import OWL, RDFS, SKOS
from sqlalchemy.orm import Session

from ontokit.git.bare_repository import BareGitRepositoryService
from ontokit.models.change_event import EntityChangeEvent
from ontokit.models.llm_generated_entity import LLMGeneratedEntity
from ontokit.models.project import Project, ProjectMember
from ontokit.models.pull_request import GitHubIntegration, PullRequest
from ontokit.models.user_commit_identity import UserCommitIdentity
from ontokit.services.commit_identity import noreply_alias

PROJECT = UUID(int=1)
BATCH = UUID(int=2)
BRANCH = "main"
PATH = "ontology.ttl"
BASE_IRI = "https://example.org/ontology#"
PREFIXES = f"""
@prefix ex: <{BASE_IRI}> .
@prefix owl: <{OWL}> .
@prefix rdfs: <{RDFS}> .
@prefix skos: <{SKOS}> .
"""


class History:
    def __init__(self, tmp_path: Path, session: Session) -> None:
        self.git = BareGitRepositoryService(str(tmp_path))
        self.repo = self.git.get_repository(PROJECT)
        self.session = session
        self.db = AsyncMock()
        self.db.begin = self.transaction
        self.db.begin_nested = self.savepoint
        self.db.add = session.add
        self.db.flush.side_effect = session.flush
        self.db.commit.side_effect = self.commit_db
        self.db.get.side_effect = session.get
        self.db.execute.side_effect = self.execute
        self.db.scalars.side_effect = session.scalars
        self.db.scalar.side_effect = self.scalar
        self.locked = False
        self.lock_available = True

    @asynccontextmanager
    async def transaction(self):
        try:
            with self.session.begin():
                yield
        finally:
            self.locked = False

    @asynccontextmanager
    async def savepoint(self):
        with self.session.begin_nested():
            yield

    def commit_db(self) -> None:
        self.session.commit()
        self.locked = False

    def execute(self, statement, params=None):
        if isinstance(statement, sa.TextClause):
            return self.scalar(statement, params)
        return self.session.execute(statement, params)

    def scalar(self, statement, params=None):
        if isinstance(statement, sa.TextClause):
            sql = str(statement)
            assert "pg_try_advisory_xact_lock" in sql or "pg_advisory_xact_lock" in sql
            assert params["lock_key"].startswith(f"suggestion:{PROJECT}:")
            if self.locked or not self.lock_available:
                return False
            self.locked = True
            return True
        return self.session.scalar(statement, params)

    def commit(
        self,
        content: str,
        *,
        branch: str = BRANCH,
        age: int = 1,
        name: str = "Remote Contributor",
        email: str = "remote@example.org",
        second_parent: str | None = None,
        filename: str = PATH,
    ) -> tuple[str, datetime]:
        git = self.repo.repo
        ref = f"refs/heads/{branch}"
        parent = git.references[ref].target if ref in git.references else None
        parents = [parent] if parent is not None else []
        if second_parent is not None:
            parents.append(pygit2.Oid(hex=second_parent))
        time = (datetime.now(UTC) - timedelta(days=age)).replace(microsecond=0)
        author = pygit2.Signature(name, email, int(time.timestamp()) - 3600, 0)
        committer = pygit2.Signature(
            "Integrator", "integrator@example.org", int(time.timestamp()), 0
        )
        tree = git.TreeBuilder()
        tree.insert(filename, git.create_blob(content.encode()), pygit2.GIT_FILEMODE_BLOB)
        oid = git.create_commit(ref, author, committer, "Ontology update", tree.write(), parents)
        return str(oid), time

    async def reconcile(self, branch: str = BRANCH, **kwargs):
        from ontokit.services.change_history_reconciler import ChangeHistoryReconciler

        return await ChangeHistoryReconciler(self.db, self.git).reconcile_branch(
            PROJECT, branch, **kwargs
        )

    def events(self) -> list[EntityChangeEvent]:
        with self.session.begin():
            return list(self.session.scalars(sa.select(EntityChangeEvent)))

    def watermark(self, branch: str = BRANCH) -> str | None:
        from ontokit.models.change_history_watermark import ChangeHistoryWatermark

        with self.session.begin():
            row = self.session.get(ChangeHistoryWatermark, (PROJECT, branch))
            return row.commit_hash if row else None


@pytest.fixture
def history(tmp_path: Path):
    from ontokit.models.change_history_watermark import ChangeHistoryWatermark

    engine = sa.create_engine("sqlite://")
    models = (
        Project,
        ProjectMember,
        GitHubIntegration,
        PullRequest,
        UserCommitIdentity,
        EntityChangeEvent,
        LLMGeneratedEntity,
        ChangeHistoryWatermark,
    )
    with engine.begin() as connection:
        for model in models:
            sa.inspect(model).local_table.create(connection)
    with Session(engine, expire_on_commit=False) as session:
        with session.begin():
            session.add(Project(id=PROJECT, name="Example ontology", owner_id="owner"))
        yield History(tmp_path, session)
    engine.dispose()


@pytest.mark.asyncio
async def test_first_parent_history_has_commit_times_and_no_duplicate_events(history: History):
    a, ta = history.commit(PREFIXES + "ex:A a owl:Class .", age=3)
    history.repo.create_branch("proposal", BRANCH)
    side, _ = history.commit(
        PREFIXES + "ex:A a owl:Class . ex:Side a owl:Class .", branch="proposal"
    )
    b, tb = history.commit(PREFIXES + "ex:A a owl:Class . ex:B a owl:Class .", age=2)
    c, tc = history.commit(
        PREFIXES + "ex:A a owl:Class . ex:B a owl:Class . ex:C a owl:Class .", second_parent=side
    )
    with patch.object(
        type(history.repo), "read_file", autospec=True, side_effect=type(history.repo).read_file
    ) as reads:
        result = await history.reconcile()
        rerun = await history.reconcile()

    assert result == {"processed": 3, "recorded": 3, "skipped": 0, "parse_errors": 0, "more": False}
    assert rerun["processed"] == 0
    assert history.watermark() == c
    assert [(args[1], args[2]) for args, _ in reads.call_args_list] == [
        (a, PATH),
        (b, PATH),
        (c, PATH),
    ]
    assert {row.commit_hash: row.created_at.replace(tzinfo=UTC) for row in history.events()} == {
        a: ta,
        b: tb,
        c: tc,
    }


@pytest.mark.asyncio
async def test_no_entity_change_advances_watermark_and_rerun_does_not_parse(history: History):
    content = PREFIXES + "ex:A a owl:Class ."
    history.commit(content)
    head, _ = history.commit(content + "\n# Same triples")
    result = await history.reconcile()
    assert result["processed"] == 2 and result["recorded"] == 1
    assert history.watermark() == head
    with patch.object(type(history.repo), "read_file") as read:
        assert (await history.reconcile())["processed"] == 0
    read.assert_not_called()


@pytest.mark.asyncio
async def test_backfill_window_and_batch_limit(history: History):
    history.commit(PREFIXES + "ex:Ancient a owl:Class .", age=120)
    baseline, _ = history.commit(PREFIXES + "ex:Baseline a owl:Class .", age=91)
    first, _ = history.commit(PREFIXES + "ex:Baseline a owl:Class . ex:A a owl:Class .", age=88)
    head, _ = history.commit(
        PREFIXES + "ex:Baseline a owl:Class . ex:A a owl:Class . ex:B a owl:Class ."
    )
    result = await history.reconcile(max_commits=1)

    assert result["processed"] == 1 and result["more"]
    assert history.watermark() == first
    assert (await history.reconcile(max_commits=1))["more"] is False
    assert history.watermark() == head
    assert {row.commit_hash for row in history.events()} == {first, head}
    assert baseline not in {row.commit_hash for row in history.events()}


@pytest.mark.asyncio
async def test_parse_error_keeps_last_good_graph_across_batch_boundary(
    history: History, caplog: pytest.LogCaptureFixture
):
    content = PREFIXES + "ex:A a owl:Class ."
    history.commit(content)
    broken, _ = history.commit("sensitive invalid ontology contents")
    head, _ = history.commit(content + "ex:B a owl:Class .")
    result = await history.reconcile(max_commits=2)

    assert result["parse_errors"] == 1 and result["more"]
    assert history.watermark() == broken
    assert (await history.reconcile())["parse_errors"] == 0
    assert history.watermark() == head
    assert [
        (row.entity_iri, row.event_type) for row in history.events() if row.commit_hash == head
    ] == [(BASE_IRI + "B", "create")]
    assert broken in caplog.text
    assert "sensitive invalid" not in caplog.text


@pytest.mark.asyncio
async def test_rewritten_branch_resets_and_nondefault_branch_starts_at_head(
    history: History, caplog: pytest.LogCaptureFixture
):
    base, _ = history.commit(PREFIXES)
    history.repo.create_branch("work", BRANCH)
    head, _ = history.commit(PREFIXES + "ex:A a owl:Class .", branch="work")
    assert (await history.reconcile("work"))["processed"] == 0
    assert history.watermark("work") == head
    history.repo.repo.references["refs/heads/work"].set_target(pygit2.Oid(hex=base))
    rewritten, _ = history.commit(PREFIXES + "ex:B a owl:Class .", branch="work")

    assert (await history.reconcile("work"))["processed"] == 0
    assert history.watermark("work") == rewritten
    assert history.events() == []
    assert "watermark" in caplog.text


@pytest.mark.asyncio
async def test_contended_branch_job_does_no_work(history: History):
    history.commit(PREFIXES + "ex:A a owl:Class .")
    history.lock_available = False
    assert (await history.reconcile())["processed"] == 0
    assert history.watermark() is None
    assert history.events() == []


@pytest.mark.asyncio
async def test_commit_attribution_and_real_ledger_origin(history: History):
    proposal = (
        PREFIXES + 'ex:Matched a owl:Class; rdfs:label "Proposal"; skos:definition "A class." .'
        'ex:Edited a owl:Class; rdfs:label "Changed" . ex:Other a owl:Class .'
    )
    merged, _ = history.commit(proposal)
    human, _ = history.commit(
        proposal + "ex:Known a owl:Class .", name="Owner", email=noreply_alias("owner", "Owner")
    )
    remote, _ = history.commit(
        proposal + "ex:Known a owl:Class . ex:Remote a owl:Class .",
        name="outside@example.org",
    )
    with history.session.begin():
        history.session.add(
            PullRequest(
                project_id=PROJECT,
                pr_number=1,
                title="Proposal",
                source_branch="proposal",
                target_branch=BRANCH,
                author_id="contributor",
                author_name="Contributor",
                merge_commit_hash=merged,
            )
        )
        for name in ("Matched", "Edited"):
            history.session.add(
                LLMGeneratedEntity(
                    project_id=PROJECT,
                    branch="proposal",
                    entity_iri=BASE_IRI + name,
                    batch_id=BATCH,
                    suggestion_type="children",
                    focus_iri=BASE_IRI + "Focus",
                    label="Proposal",
                    definition=" A   class. " if name == "Matched" else None,
                    model="example-model",
                    user_id="contributor",
                )
            )
        # Legacy synthetic PRs put the Git email in the author ID.
        history.session.add(
            PullRequest(
                project_id=PROJECT,
                pr_number=2,
                title="Legacy",
                source_branch="remote",
                target_branch=BRANCH,
                author_id="outside@example.org",
                author_name="outside@example.org",
                merge_commit_hash=remote,
            )
        )

    assert (await history.reconcile())["recorded"] == 5
    rows = {row.entity_iri.removeprefix(BASE_IRI): row for row in history.events()}
    assert {
        (rows[name].commit_hash, rows[name].user_id, rows[name].user_name)
        for name in ("Matched", "Edited", "Other")
    } == {(merged, "contributor", "Contributor")}
    assert (
        rows["Matched"].origin,
        rows["Matched"].origin_model,
        rows["Matched"].origin_batch_id,
    ) == (
        "ai",
        "example-model",
        BATCH,
    )
    assert rows["Edited"].origin == "ai_edited"
    assert rows["Other"].origin == "human"
    assert (rows["Known"].commit_hash, rows["Known"].user_id, rows["Known"].origin) == (
        human,
        "owner",
        "human",
    )
    assert (rows["Remote"].user_id, rows["Remote"].user_name, rows["Remote"].origin) == (
        "system",
        None,
        "system",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("pending", [None, "gap", "initial", "suggestion"])
async def test_direct_save_preserves_ai_provenance_and_watermark_contiguity(
    history: History, pending: str | None
):
    from types import SimpleNamespace

    from ontokit.api.routes.projects import save_source_content
    from ontokit.core.auth import CurrentUser
    from ontokit.schemas.project import SourceContentSave
    from ontokit.services.change_event_service import ChangeEventService
    from ontokit.services.ontology import OntologyService

    base, _ = history.commit(PREFIXES)
    if pending != "initial":
        await history.reconcile()
    previous = base
    branch = BRANCH
    old_content = PREFIXES
    if pending in ("gap", "initial"):
        old_content += "ex:Pending a owl:Class ."
        previous, _ = history.commit(old_content)
    elif pending == "suggestion":
        branch = "suggest/editor/session"
        history.repo.create_branch(branch, BRANCH)

    with history.session.begin():
        for name in ("Matched", "Edited"):
            history.session.add(
                LLMGeneratedEntity(
                    project_id=PROJECT,
                    branch=branch,
                    entity_iri=BASE_IRI + name,
                    batch_id=BATCH,
                    suggestion_type="children",
                    focus_iri=BASE_IRI + "Focus",
                    label="Proposal",
                    definition="A class." if name == "Matched" else None,
                    model="example-model",
                    user_id="owner",
                )
            )

    project = SimpleNamespace(user_role="owner", source_file_path=PATH, git_ontology_path=None)
    content = old_content + (
        'ex:Matched a owl:Class; rdfs:label "Proposal"; skos:definition "A class." .'
        'ex:Edited a owl:Class; rdfs:label "Changed" . ex:Other a owl:Class .'
    )
    with (
        patch("ontokit.api.routes.projects.get_arq_pool", AsyncMock(return_value=None)),
        patch("ontokit.services.translation_jobs.enqueue_label_diff_after_commit", AsyncMock()),
    ):
        saved = await save_source_content(
            PROJECT,
            SourceContentSave(
                content=content, commit_message="Source save", base_revision=previous
            ),
            history.db,
            SimpleNamespace(get=AsyncMock(return_value=project)),
            SimpleNamespace(upload_file=AsyncMock()),
            OntologyService(),
            history.git,
            ChangeEventService(history.db),
            SimpleNamespace(get_config=AsyncMock(return_value=None)),
            CurrentUser(id="owner", name="Owner", email="owner@example.org"),
            branch=branch,
        )

    if pending == "suggestion":
        assert history.watermark(branch) is None
        assert history.events() == []
        return

    assert history.watermark() == ({"gap": base, "initial": None}.get(pending, saved.commit_hash))
    rows = {row.entity_iri.removeprefix(BASE_IRI): row for row in history.events()}
    assert {(name, row.origin) for name, row in rows.items()} == {
        ("Matched", "ai"),
        ("Edited", "ai_edited"),
        ("Other", "human"),
    }
    assert (rows["Matched"].origin_model, rows["Matched"].origin_batch_id) == (
        "example-model",
        BATCH,
    )
    if pending:
        caught_up = await history.reconcile()
        assert caught_up["recorded"] == 1 and caught_up["skipped"] == 1
        assert history.watermark() == saved.commit_hash


@pytest.mark.asyncio
async def test_watermark_reachable_through_second_parent_catches_up(history: History):
    content = PREFIXES + "ex:Base a owl:Class ."
    history.commit(content)
    history.repo.create_branch("other", BRANCH)
    recorded, _ = history.commit(content + "ex:Local a owl:Class .")
    await history.reconcile()
    history.commit(content + "ex:Remote a owl:Class .", branch="other")
    joined, _ = history.commit(
        content + "ex:Local a owl:Class . ex:Remote a owl:Class .",
        branch="other",
        second_parent=recorded,
    )
    history.repo.repo.references["refs/heads/main"].set_target(pygit2.Oid(hex=joined))

    assert (await history.reconcile())["processed"] == 1
    assert history.watermark() == joined
    events = [row for row in history.events() if row.commit_hash == joined]
    assert [(row.entity_iri, row.event_type) for row in events] == [(BASE_IRI + "Remote", "create")]


@pytest.mark.asyncio
async def test_concurrent_reconciliation_returns_while_first_run_holds_lock(history: History):
    from ontokit.services.change_history_reconciler import ChangeHistoryReconciler

    history.commit(PREFIXES + "ex:Class a owl:Class .")
    locked = asyncio.Event()
    release = asyncio.Event()
    original_get = history.db.get.side_effect

    async def pause_locked_run(*args, **kwargs):
        if args[0] is Project:
            locked.set()
            await release.wait()
        return original_get(*args, **kwargs)

    @asynccontextmanager
    async def second_transaction():
        yield

    second_db = AsyncMock()
    second_db.begin = second_transaction
    second_db.scalar.side_effect = history.scalar
    history.db.get.side_effect = pause_locked_run
    first = asyncio.create_task(history.reconcile())
    try:
        await locked.wait()
        second = await ChangeHistoryReconciler(second_db, history.git).reconcile_branch(
            PROJECT, BRANCH
        )
        assert second["processed"] == 0
        second_db.get.assert_not_awaited()
    finally:
        release.set()
        assert (await first)["recorded"] == 1

    assert len(history.events()) == 1


@pytest.mark.asyncio
async def test_jsonld_history_parses_inline_context_without_fetching_remote_context(
    history: History,
):
    from rdflib.plugins.shared.jsonld.context import Context

    filename = "ontology.jsonld"
    with history.session.begin():
        history.session.add(
            GitHubIntegration(
                project_id=PROJECT,
                repo_owner="example",
                repo_name="ontology",
                ontology_file_path=filename,
                default_branch=BRANCH,
                connected_by_user_id="owner",
            )
        )
    content = {"@id": BASE_IRI + "Local", "@type": str(OWL.Class)}
    good, _ = history.commit(
        json.dumps({"@context": {"owl": str(OWL)}, **content}), filename=filename
    )
    remote, _ = history.commit(
        json.dumps({"@context": "https://example.org/context", **content}), filename=filename
    )
    with patch.object(
        Context, "_fetch_context", side_effect=RuntimeError("Remote context")
    ) as fetch:
        result = await history.reconcile()

    fetch.assert_not_called()
    assert result["parse_errors"] == 1
    assert history.watermark() == remote
    assert [row.commit_hash for row in history.events()] == [good]


@pytest.mark.asyncio
async def test_running_job_reports_more_when_another_landing_advances_the_ref(history: History):
    original, _ = history.commit(PREFIXES + "ex:A a owl:Class .")
    read_scalar = history.scalar

    def land_during_reconciliation(statement, params=None):
        if (
            isinstance(statement, sa.Select)
            and statement.column_descriptions[0]["entity"] is EntityChangeEvent
        ):
            history.db.scalar.side_effect = read_scalar
            history.commit(PREFIXES + "ex:A a owl:Class . ex:B a owl:Class .")
        return read_scalar(statement, params)

    history.db.scalar.side_effect = land_during_reconciliation

    result = await history.reconcile()

    assert result["processed"] == 1 and result["more"]
    assert history.watermark() == original
    assert (await history.reconcile())["recorded"] == 1
